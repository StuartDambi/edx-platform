""" Django admin pages for student app """


from functools import wraps

from config_models.admin import ConfigurationModelAdmin
from django import forms
from django.contrib import admin
from django.contrib.admin.sites import NotRegistered
from django.contrib.admin.utils import unquote
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm
from django.db import models, router, transaction
from django.http import HttpResponseRedirect
from django.http.request import QueryDict
from django.urls import reverse
from django.utils.translation import ngettext
from django.utils.translation import ugettext_lazy as _
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from openedx.core.djangoapps.waffle_utils import WaffleSwitch
from openedx.core.lib.courses import clean_course_id
from student import STUDENT_WAFFLE_NAMESPACE
from student.models import (
    AccountRecovery,
    AllowedAuthUser,
    CourseAccessRole,
    CourseEnrollment,
    CourseEnrollmentAllowed,
    DashboardConfiguration,
    LinkedInAddToProfileConfiguration,
    LoginFailures,
    PendingNameChange,
    Registration,
    RegistrationCookieConfiguration,
    UserAttribute,
    UserProfile,
    UserTestGroup,
    BulkUnenrollConfiguration
)
from student.roles import REGISTERED_ACCESS_ROLES
from xmodule.modulestore.django import modulestore

User = get_user_model()  # pylint:disable=invalid-name

# This switch exists because the CourseEnrollment admin views make DB queries that impact performance.
# In a large enough deployment of Open edX, this is enough to cause a site outage.
# See https://openedx.atlassian.net/browse/OPS-2943
COURSE_ENROLLMENT_ADMIN_SWITCH = WaffleSwitch(STUDENT_WAFFLE_NAMESPACE, 'courseenrollment_admin')


class CourseAccessRoleForm(forms.ModelForm):
    """Form for adding new Course Access Roles view the Django Admin Panel."""

    class Meta(object):
        model = CourseAccessRole
        fields = '__all__'

    email = forms.EmailField(required=True)
    COURSE_ACCESS_ROLES = [(role_name, role_name) for role_name in REGISTERED_ACCESS_ROLES.keys()]
    role = forms.ChoiceField(choices=COURSE_ACCESS_ROLES)

    def clean_course_id(self):
        """
        Validate the course id
        """
        if self.cleaned_data['course_id']:
            return clean_course_id(self)

    def clean_org(self):
        """If org and course-id exists then Check organization name
        against the given course.
        """
        if self.cleaned_data.get('course_id') and self.cleaned_data['org']:
            org = self.cleaned_data['org']
            org_name = self.cleaned_data.get('course_id').org
            if org.lower() != org_name.lower():
                raise forms.ValidationError(
                    u"Org name {} is not valid. Valid name is {}.".format(
                        org, org_name
                    )
                )

        return self.cleaned_data['org']

    def clean_email(self):
        """
        Checking user object against given email id.
        """
        email = self.cleaned_data['email']
        try:
            user = User.objects.get(email=email)
        except Exception:
            raise forms.ValidationError(
                u"Email does not exist. Could not find {email}. Please re-enter email address".format(
                    email=email
                )
            )

        return user

    def clean(self):
        """
        Checking the course already exists in db.
        """
        cleaned_data = super(CourseAccessRoleForm, self).clean()
        if not self.errors:
            if CourseAccessRole.objects.filter(
                    user=cleaned_data.get("email"),
                    org=cleaned_data.get("org"),
                    course_id=cleaned_data.get("course_id"),
                    role=cleaned_data.get("role")
            ).exists():
                raise forms.ValidationError("Duplicate Record.")

        return cleaned_data

    def __init__(self, *args, **kwargs):
        super(CourseAccessRoleForm, self).__init__(*args, **kwargs)
        if self.instance.user_id:
            self.fields['email'].initial = self.instance.user.email


@admin.register(CourseAccessRole)
class CourseAccessRoleAdmin(admin.ModelAdmin):
    """Admin panel for the Course Access Role. """
    form = CourseAccessRoleForm
    raw_id_fields = ("user",)
    exclude = ("user",)

    fieldsets = (
        (None, {
            'fields': ('email', 'course_id', 'org', 'role',)
        }),
    )

    list_display = (
        'id', 'user', 'org', 'course_id', 'role',
    )
    search_fields = (
        'id', 'user__username', 'user__email', 'org', 'course_id', 'role',
    )

    def save_model(self, request, obj, form, change):
        obj.user = form.cleaned_data['email']
        super(CourseAccessRoleAdmin, self).save_model(request, obj, form, change)


@admin.register(LinkedInAddToProfileConfiguration)
class LinkedInAddToProfileConfigurationAdmin(admin.ModelAdmin):
    """Admin interface for the LinkedIn Add to Profile configuration. """

    class Meta(object):
        model = LinkedInAddToProfileConfiguration

    # Exclude deprecated fields
    exclude = ('dashboard_tracking_code',)


class CourseEnrollmentForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        # If args is a QueryDict, then the ModelForm addition request came in as a POST with a course ID string.
        # Change the course ID string to a CourseLocator object by copying the QueryDict to make it mutable.
        if args and 'course' in args[0] and isinstance(args[0], QueryDict):
            args_copy = args[0].copy()
            try:
                args_copy['course'] = CourseKey.from_string(args_copy['course'])
            except InvalidKeyError:
                raise forms.ValidationError("Cannot make a valid CourseKey from id {}!".format(args_copy['course']))
            args = [args_copy]

        super(CourseEnrollmentForm, self).__init__(*args, **kwargs)

        if self.data.get('course'):
            try:
                self.data['course'] = CourseKey.from_string(self.data['course'])
            except AttributeError:
                # Change the course ID string to a CourseLocator.
                # On a POST request, self.data is a QueryDict and is immutable - so this code will fail.
                # However, the args copy above before the super() call handles this case.
                pass

    def clean_course_id(self):
        course_id = self.cleaned_data['course']
        try:
            course_key = CourseKey.from_string(course_id)
        except InvalidKeyError:
            raise forms.ValidationError("Cannot make a valid CourseKey from id {}!".format(course_id))

        if not modulestore().has_course(course_key):
            raise forms.ValidationError("Cannot find course with id {} in the modulestore".format(course_id))

        return course_key

    def save(self, *args, **kwargs):
        course_enrollment = super(CourseEnrollmentForm, self).save(commit=False)
        user = self.cleaned_data['user']
        course_overview = self.cleaned_data['course']
        enrollment = CourseEnrollment.get_or_create_enrollment(user, course_overview.id)
        course_enrollment.id = enrollment.id
        course_enrollment.created = enrollment.created
        return course_enrollment

    class Meta:
        model = CourseEnrollment
        fields = '__all__'


@admin.register(CourseEnrollment)
class CourseEnrollmentAdmin(admin.ModelAdmin):
    """ Admin interface for the CourseEnrollment model. """
    list_display = ('id', 'course_id', 'mode', 'user', 'is_active',)
    list_filter = ('mode', 'is_active',)
    raw_id_fields = ('user', 'course')
    search_fields = ('course__id', 'mode', 'user__username',)
    form = CourseEnrollmentForm

    def get_search_results(self, request, queryset, search_term):
        qs, use_distinct = super(CourseEnrollmentAdmin, self).get_search_results(request, queryset, search_term)

        # annotate each enrollment with whether the username was an
        # exact match for the search term
        qs = qs.annotate(exact_username_match=models.Case(
            models.When(user__username=search_term, then=models.Value(True)),
            default=models.Value(False),
            output_field=models.BooleanField()))

        # present exact matches first
        qs = qs.order_by('-exact_username_match', 'user__username', 'course_id')

        return qs, use_distinct

    def queryset(self, request):
        return super(CourseEnrollmentAdmin, self).queryset(request).select_related('user')

    def has_permission(self, request, method):
        """
        Returns True if the given admin method is allowed.
        """
        if COURSE_ENROLLMENT_ADMIN_SWITCH.is_enabled():
            return getattr(super(CourseEnrollmentAdmin, self), method)(request)
        return False

    def has_add_permission(self, request):
        """
        Returns True if CourseEnrollment objects can be added via the admin view.
        """
        return self.has_permission(request, 'has_add_permission')

    def has_change_permission(self, request, obj=None):
        """
        Returns True if CourseEnrollment objects can be modified via the admin view.
        """
        return self.has_permission(request, 'has_change_permission')

    def has_delete_permission(self, request, obj=None):
        """
        Returns True if CourseEnrollment objects can be deleted via the admin view.
        """
        return self.has_permission(request, 'has_delete_permission')

    def has_module_permission(self, request):
        """
        Returns True if links to the CourseEnrollment admin view can be displayed.
        """
        return self.has_permission(request, 'has_module_permission')


class UserProfileInline(admin.StackedInline):
    """ Inline admin interface for UserProfile model. """
    model = UserProfile
    can_delete = False
    verbose_name_plural = _('User profile')


class AccountRecoveryInline(admin.StackedInline):
    """ Inline admin interface for AccountRecovery model. """
    model = AccountRecovery
    can_delete = False
    verbose_name = _('Account recovery')
    verbose_name_plural = _('Account recovery')


class UserChangeForm(BaseUserChangeForm):
    """
    Override the default UserChangeForm such that the password field
    does not contain a link to a 'change password' form.
    """
    password = ReadOnlyPasswordHashField(
        label=_("Password"),
        help_text=_(
            "Raw passwords are not stored, so there is no way to see this "
            "user's password."
        ),
    )


class UserAdmin(BaseUserAdmin):
    """ Admin interface for the User model. """
    inlines = (UserProfileInline, AccountRecoveryInline)
    form = UserChangeForm

    def get_readonly_fields(self, request, obj=None):
        """
        Allows editing the users while skipping the username check, so we can have Unicode username with no problems.
        The username is marked read-only when editing existing users regardless of `ENABLE_UNICODE_USERNAME`, to simplify the bokchoy tests.
        """
        django_readonly = super(UserAdmin, self).get_readonly_fields(request, obj)
        if obj:
            return django_readonly + ('username',)
        return django_readonly


@admin.register(UserAttribute)
class UserAttributeAdmin(admin.ModelAdmin):
    """ Admin interface for the UserAttribute model. """
    list_display = ('user', 'name', 'value',)
    list_filter = ('name',)
    raw_id_fields = ('user',)
    search_fields = ('name', 'value', 'user__username',)

    class Meta(object):
        model = UserAttribute


@admin.register(CourseEnrollmentAllowed)
class CourseEnrollmentAllowedAdmin(admin.ModelAdmin):
    """ Admin interface for the CourseEnrollmentAllowed model. """
    list_display = ('email', 'course_id', 'auto_enroll',)
    search_fields = ('email', 'course_id',)

    class Meta(object):
        model = CourseEnrollmentAllowed


@admin.register(LoginFailures)
class LoginFailuresAdmin(admin.ModelAdmin):
    """Admin interface for the LoginFailures model. """
    list_display = ('user', 'failure_count', 'lockout_until')
    raw_id_fields = ('user',)
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    actions = ['unlock_student_accounts']
    change_form_template = 'admin/student/loginfailures/change_form_template.html'

    class _Feature(object):
        """
        Inner feature class to implement decorator.
        """
        @classmethod
        def is_enabled(cls, func):
            """
            Check if feature is enabled.
            """
            @wraps(func)
            def decorator(*args, **kwargs):
                """Decorator class to return"""
                if not LoginFailures.is_feature_enabled():
                    return False
                return func(*args, **kwargs)
            return decorator

    @_Feature.is_enabled
    def has_module_permission(self, request):
        """
        Only enabled if feature is enabled.
        """
        return super(LoginFailuresAdmin, self).has_module_permission(request)

    @_Feature.is_enabled
    def has_delete_permission(self, request, obj=None):
        """
        Only enabled if feature is enabled.
        """
        return super(LoginFailuresAdmin, self).has_delete_permission(request, obj)

    @_Feature.is_enabled
    def has_change_permission(self, request, obj=None):
        """
        Only enabled if feature is enabled.
        """
        return super(LoginFailuresAdmin, self).has_change_permission(request, obj)

    @_Feature.is_enabled
    def has_add_permission(self, request):
        """
        Only enabled if feature is enabled.
        """
        return super(LoginFailuresAdmin, self).has_add_permission(request)

    def unlock_student_accounts(self, request, queryset):
        """
        Unlock student accounts with login failures.
        """
        count = 0
        with transaction.atomic(using=router.db_for_write(self.model)):
            for obj in queryset:
                self.unlock_student(request, obj=obj)
                count += 1
        self.message_user(
            request,
            ngettext(
                '%(count)d student account was unlocked.',
                '%(count)d student accounts were unlocked.',
                count
            ) % {
                'count': count
            }
        )

    def change_view(self, request, object_id, form_url='', extra_context=None):
        """
        Change View.

        This is overridden so we can add a custom button to unlock an account in the record's details.
        """
        if '_unlock' in request.POST:
            with transaction.atomic(using=router.db_for_write(self.model)):
                self.unlock_student(request, object_id=object_id)
                url = reverse('admin:student_loginfailures_changelist', current_app=self.admin_site.name)
                return HttpResponseRedirect(url)
        return super(LoginFailuresAdmin, self).change_view(request, object_id, form_url, extra_context)

    def get_actions(self, request):
        """
        Get actions for model admin and remove delete action.
        """
        actions = super(LoginFailuresAdmin, self).get_actions(request)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

    def unlock_student(self, request, object_id=None, obj=None):
        """
        Unlock student account.
        """
        if object_id:
            obj = self.get_object(request, unquote(object_id))

        self.model.clear_lockout_counter(obj.user)


class AllowedAuthUserForm(forms.ModelForm):
    """Model Form for AllowedAuthUser model's admin interface."""

    class Meta(object):
        model = AllowedAuthUser
        fields = ('site', 'email', )

    def clean_email(self):
        """
        Validate the email field.
        """
        email = self.cleaned_data['email']
        email_domain = email.split('@')[-1]
        allowed_site_email_domain = self.cleaned_data['site'].configuration.get_value('THIRD_PARTY_AUTH_ONLY_DOMAIN')

        if not allowed_site_email_domain:
            raise forms.ValidationError(
                _("Please add a key/value 'THIRD_PARTY_AUTH_ONLY_DOMAIN/{site_email_domain}' in SiteConfiguration "
                  "model's site_values field.")
            )
        elif email_domain != allowed_site_email_domain:
            raise forms.ValidationError(
                _("Email doesn't have {domain_name} domain name.".format(domain_name=allowed_site_email_domain))
            )
        elif not User.objects.filter(email=email).exists():
            raise forms.ValidationError(_("User with this email doesn't exist in system."))
        else:
            return email


@admin.register(AllowedAuthUser)
class AllowedAuthUserAdmin(admin.ModelAdmin):
    """ Admin interface for the AllowedAuthUser model. """
    form = AllowedAuthUserForm
    list_display = ('email', 'site',)
    search_fields = ('email',)
    ordering = ('-created',)

    class Meta(object):
        model = AllowedAuthUser


admin.site.register(UserTestGroup)
admin.site.register(Registration)
admin.site.register(PendingNameChange)
admin.site.register(DashboardConfiguration, ConfigurationModelAdmin)
admin.site.register(RegistrationCookieConfiguration, ConfigurationModelAdmin)
admin.site.register(BulkUnenrollConfiguration, ConfigurationModelAdmin)


# We must first un-register the User model since it may also be registered by the auth app.
try:
    admin.site.unregister(User)
except NotRegistered:
    pass

admin.site.register(User, UserAdmin)
