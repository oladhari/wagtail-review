import random
import string

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case, Value, When
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

import swapper

from wagtail import VERSION as WAGTAIL_VERSION
from wagtail.admin.mail import send_mail

if WAGTAIL_VERSION >= (5, 1):
    from wagtail.permission_policies.pages import PagePermissionPolicy
else:
    from wagtail.models import UserPagePermissionsProxy

from wagtail_review.text import user_display_name


# make the setting name WAGTAILREVIEW_REVIEW_MODEL rather than WAGTAIL_REVIEW_REVIEW_MODEL
swapper.set_app_prefix('wagtail_review', 'wagtailreview')


REVIEW_STATUS_CHOICES = [
    ('open', _("Open")),
    ('closed', _("Closed")),
]


revision_model = "wagtailcore.Revision"
revision_page_fk_relation = "page_revision__object_id"

class BaseReview(models.Model):
    """
    Abstract base class for Review models. Can be subclassed to specify application-specific fields, e.g. review type
    """
    page_revision = models.ForeignKey(revision_model, related_name='+', on_delete=models.CASCADE, editable=False)
    status = models.CharField(max_length=30, default='open', choices=REVIEW_STATUS_CHOICES, editable=False)
    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+', editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def send_request_emails(self):
        # send request emails to all reviewers except the reviewer record for the user submitting the request
        for reviewer in self.reviewers.exclude(user=self.submitter):
            reviewer.send_request_email()

    @cached_property
    def revision_as_page(self):
        return self.page_revision.as_object()

    def get_annotations(self):
        return Annotation.objects.filter(reviewer__review=self).prefetch_related('ranges')

    def get_responses(self):
        return Response.objects.filter(reviewer__review=self).order_by('created_at').select_related('reviewer')

    def get_non_responding_reviewers(self):
        return self.reviewers.filter(responses__isnull=True).exclude(user=self.submitter)

    @classmethod
    def get_pages_with_reviews_for_user(cls, user):
        """
        Return a queryset of pages which have reviews, for which the user has edit permission.
        Optimized to improve performance with large datasets.
        """
        if WAGTAIL_VERSION >= (5, 1):
            editable_pages = PagePermissionPolicy().instances_user_has_permission_for(user, "change")
        else:
            editable_pages = UserPagePermissionsProxy(user).editable_pages()

        # Get reviewed page IDs
        reviewed_page_ids = cls.objects.values_list(revision_page_fk_relation, flat=True).distinct()

        # Fetch the latest review creation date for each page
        latest_reviews = (
            cls.objects.filter(**{f"{revision_page_fk_relation}__in": reviewed_page_ids})
            .values(revision_page_fk_relation)
            .annotate(last_review_requested_at=models.Max("created_at"))
        )

        # Map page IDs to their latest review date
        latest_reviews_map = {
            review[revision_page_fk_relation]: review["last_review_requested_at"]
            for review in latest_reviews
        }

        # Add `last_review_requested_at` annotation to editable pages
        pages_with_reviews = (
            editable_pages.filter(pk__in=reviewed_page_ids)
            .annotate(
                last_review_requested_at=models.Case(
                    *[
                        models.When(pk=pk, then=models.Value(created_at))
                        for pk, created_at in latest_reviews_map.items()
                    ],
                    output_field=models.DateTimeField(),
                )
            )
            .order_by("-last_review_requested_at")
        )

        return pages_with_reviews

    class Meta:
        abstract = True


class Review(BaseReview):
    class Meta:
        swappable = swapper.swappable_setting('wagtail_review', 'Review')


def generate_token():
    return ''.join(random.SystemRandom().choice(string.ascii_lowercase + string.digits) for _ in range(16))


class Reviewer(models.Model):
    review = models.ForeignKey(swapper.get_model_name('wagtail_review', 'Review'), related_name='reviewers', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    email = models.EmailField(blank=True)
    response_token = models.CharField(
        max_length=32, editable=False,
        help_text="Secret token this user must supply to be allowed to respond to the review"
    )
    view_token = models.CharField(
        max_length=32, editable=False,
        help_text="Secret token this user must supply to be allowed to view the page revision being reviewed"
    )

    def clean(self):
        if self.user is None and not self.email:
            raise ValidationError("A reviewer must have either an email address or a user account")

    def get_email_address(self):
        return self.email or self.user.email

    def get_name(self):
        return user_display_name(self.user) if self.user else self.email

    def save(self, **kwargs):
        if not self.response_token:
            self.response_token = generate_token()
        if not self.view_token:
            self.view_token = generate_token()

        super().save(**kwargs)

    def get_respond_url(self, absolute=False):
        url = reverse('wagtail_review:respond', args=[self.id, self.response_token])
        if absolute:
            url = settings.WAGTAILADMIN_BASE_URL + url
        return url

    def get_view_url(self, absolute=False):
        url = reverse('wagtail_review:view', args=[self.id, self.view_token])
        if absolute:
            url = settings.WAGTAILADMIN_BASE_URL + url
        return url

    def send_request_email(self):
        email_address = self.get_email_address()

        context = {
            'email': email_address,
            'user': self.user,
            'review': self.review,
            'page': self.review.revision_as_page,
            'submitter': self.review.submitter,
            'respond_url': self.get_respond_url(absolute=True),
            'view_url': self.get_view_url(absolute=True),
        }

        email_subject = render_to_string('wagtail_review/email/request_review_subject.txt', context).strip()
        email_content = render_to_string('wagtail_review/email/request_review.txt', context).strip()

        send_mail(email_subject, email_content, [email_address])


class Annotation(models.Model):
    reviewer = models.ForeignKey(Reviewer, related_name='annotations', on_delete=models.CASCADE)
    quote = models.TextField(blank=True)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def as_json_data(self):
        return {
            'id': self.id,
            'annotator_schema_version': 'v1.0',
            'created': self.created_at.isoformat(),
            'updated': self.updated_at.isoformat(),
            'text': self.text,
            'quote': self.quote,
            'user': {
                'id': self.reviewer.id,
                'name': self.reviewer.get_name(),
            },
            'ranges': [r.as_json_data() for r in self.ranges.all()],
        }


class AnnotationRange(models.Model):
    annotation = models.ForeignKey(Annotation, related_name='ranges', on_delete=models.CASCADE)
    start = models.TextField()
    start_offset = models.IntegerField()
    end = models.TextField()
    end_offset = models.IntegerField()

    def as_json_data(self):
        return {
            'start': self.start,
            'startOffset': self.start_offset,
            'end': self.end,
            'endOffset': self.end_offset,
        }


RESULT_CHOICES = (
    ('approve', 'Approved'),
    ('comment', 'Comment'),
)


class Response(models.Model):
    reviewer = models.ForeignKey(Reviewer, related_name='responses', on_delete=models.CASCADE)
    result = models.CharField(choices=RESULT_CHOICES, max_length=10, blank=False, default=None)
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def send_notification_to_submitter(self):
        submitter = self.reviewer.review.submitter
        if submitter.email:

            context = {
                'submitter': submitter,
                'reviewer': self.reviewer,
                'review': self.reviewer.review,
                'page': self.reviewer.review.revision_as_page,
                'response': self,
            }

            email_subject = render_to_string('wagtail_review/email/response_received_subject.txt', context).strip()
            email_content = render_to_string('wagtail_review/email/response_received.txt', context).strip()

            send_mail(email_subject, email_content, [submitter.email])
