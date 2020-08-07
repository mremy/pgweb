from django.contrib.auth.models import User
from django.contrib.auth import login as django_login
import django.contrib.auth.views as authviews
from django.http import HttpResponseRedirect, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from pgweb.util.decorators import login_required, script_sources, frame_sources
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth import logout as django_logout
from django.conf import settings
from django.db import transaction, connection
from django.db.models import Q, Prefetch

import base64
import urllib.parse
from Cryptodome.Cipher import AES
from Cryptodome import Random
import time
import json
from datetime import datetime, timedelta
import itertools

from pgweb.util.contexts import render_pgweb
from pgweb.util.misc import send_template_mail, generate_random_token, get_client_ip
from pgweb.util.helpers import HttpSimpleResponse

from pgweb.news.models import NewsArticle
from pgweb.events.models import Event
from pgweb.core.models import Organisation, UserProfile
from pgweb.contributors.models import Contributor
from pgweb.downloads.models import Product
from pgweb.profserv.models import ProfessionalService

from .models import CommunityAuthSite, CommunityAuthConsent, SecondaryEmail
from .forms import PgwebAuthenticationForm
from .forms import CommunityAuthConsentForm
from .forms import SignupForm, SignupOauthForm
from .forms import UserForm, UserProfileForm, ContributorForm
from .forms import AddEmailForm, PgwebPasswordResetForm

import logging
log = logging.getLogger(__name__)

# The value we store in user.password for oauth logins. This is
# a value that must not match any hashers.
OAUTH_PASSWORD_STORE = 'oauth_signin_account_no_password'


@login_required
def home(request):
    myarticles = NewsArticle.objects.filter(org__managers=request.user, approved=False)
    myevents = Event.objects.filter(org__managers=request.user, approved=False)
    myorgs = Organisation.objects.filter(managers=request.user, approved=False)
    myproducts = Product.objects.filter(org__managers=request.user, approved=False)
    myprofservs = ProfessionalService.objects.filter(org__managers=request.user, approved=False)
    return render_pgweb(request, 'account', 'account/index.html', {
        'newsarticles': myarticles,
        'events': myevents,
        'organisations': myorgs,
        'products': myproducts,
        'profservs': myprofservs,
    })


objtypes = {
    'news': {
        'title': 'News Article',
        'objects': lambda u: NewsArticle.objects.filter(org__managers=u),
    },
    'events': {
        'title': 'Event',
        'objects': lambda u: Event.objects.filter(org__managers=u),
    },
    'products': {
        'title': 'Product',
        'objects': lambda u: Product.objects.filter(org__managers=u),
    },
    'services': {
        'title': 'Professional Service',
        'objects': lambda u: ProfessionalService.objects.filter(org__managers=u),
    },
    'organisations': {
        'title': 'Organisation',
        'objects': lambda u: Organisation.objects.filter(managers=u),
        'submit_header': 'Before submitting a new Organisation, please verify on the list of <a href="/account/orglist/">current organisations</a> if the organisation already exists. If it does, please contact the manager of the organisation to gain permissions.',
    },
}


@login_required
@transaction.atomic
def profile(request):
    # We always have the user, but not always the profile. And we need a bit
    # of a hack around the normal forms code since we have two different
    # models on a single form.
    (profile, created) = UserProfile.objects.get_or_create(pk=request.user.pk)

    # Don't allow users whose accounts were created via oauth to change
    # their email, since that would kill the connection between the
    # accounts.
    can_change_email = (request.user.password != OAUTH_PASSWORD_STORE)

    # We may have a contributor record - and we only show that part of the
    # form if we have it for this user.
    try:
        contrib = Contributor.objects.get(user=request.user.pk)
    except Contributor.DoesNotExist:
        contrib = None

    contribform = None

    secondaryaddresses = SecondaryEmail.objects.filter(user=request.user)

    if request.method == 'POST':
        # Process this form
        userform = UserForm(can_change_email, secondaryaddresses, data=request.POST, instance=request.user)
        profileform = UserProfileForm(data=request.POST, instance=profile)
        secondaryemailform = AddEmailForm(request.user, data=request.POST)
        if contrib:
            contribform = ContributorForm(data=request.POST, instance=contrib)

        if userform.is_valid() and profileform.is_valid() and secondaryemailform.is_valid() and (not contrib or contribform.is_valid()):
            user = userform.save()

            # Email takes some magic special handling, since we only allow picking of existing secondary emails, but it's
            # not a foreign key (due to how the django auth model works).
            if can_change_email and userform.cleaned_data['primaryemail'] != user.email:
                # Changed it!
                oldemail = user.email
                # Create a secondary email for the old primary one
                SecondaryEmail(user=user, email=oldemail, confirmed=True, token='').save()
                # Flip the main email
                user.email = userform.cleaned_data['primaryemail']
                user.save(update_fields=['email', ])
                # Finally remove the old secondary address, since it can`'t be both primary and secondary at the same time
                SecondaryEmail.objects.filter(user=user, email=user.email).delete()
                log.info("User {} changed primary email from {} to {}".format(user.username, oldemail, user.email))

            profileform.save()
            if contrib:
                contribform.save()
            if secondaryemailform.cleaned_data.get('email1', ''):
                sa = SecondaryEmail(user=request.user, email=secondaryemailform.cleaned_data['email1'], token=generate_random_token())
                sa.save()
                send_template_mail(
                    settings.ACCOUNTS_NOREPLY_FROM,
                    sa.email,
                    'Your postgresql.org community account',
                    'account/email_add_email.txt',
                    {'secondaryemail': sa, 'user': request.user, }
                )

            for k, v in request.POST.items():
                if k.startswith('deladdr_') and v == '1':
                    ii = int(k[len('deladdr_'):])
                    SecondaryEmail.objects.filter(user=request.user, id=ii).delete()

            return HttpResponseRedirect(".")
    else:
        # Generate form
        userform = UserForm(can_change_email, secondaryaddresses, instance=request.user)
        profileform = UserProfileForm(instance=profile)
        secondaryemailform = AddEmailForm(request.user)
        if contrib:
            contribform = ContributorForm(instance=contrib)

    return render_pgweb(request, 'account', 'account/userprofileform.html', {
        'userform': userform,
        'profileform': profileform,
        'secondaryemailform': secondaryemailform,
        'secondaryaddresses': secondaryaddresses,
        'secondarypending': any(not a.confirmed for a in secondaryaddresses),
        'contribform': contribform,
    })


@login_required
@transaction.atomic
def confirm_add_email(request, tokenhash):
    addr = get_object_or_404(SecondaryEmail, user=request.user, token=tokenhash)

    # Valid token found, so mark the address as confirmed.
    addr.confirmed = True
    addr.token = ''
    addr.save()
    return HttpResponseRedirect('/account/profile/')


@login_required
def listobjects(request, objtype):
    if objtype not in objtypes:
        raise Http404("Object type not found")
    o = objtypes[objtype]

    return render_pgweb(request, 'account', 'account/objectlist.html', {
        'objects': {
            'approved': o['objects'](request.user).filter(approved=True),
            'unapproved': o['objects'](request.user).filter(approved=False),
        },
        'title': o['title'],
        'submit_header': o.get('submit_header', None),
        'suburl': objtype,
    })


@login_required
def orglist(request):
    orgs = Organisation.objects.filter(approved=True)

    return render_pgweb(request, 'account', 'account/orglist.html', {
        'orgs': orgs,
    })


def login(request):
    return authviews.LoginView.as_view(template_name='account/login.html',
                                       authentication_form=PgwebAuthenticationForm,
                                       extra_context={
                                           'oauth_providers': [(k, v) for k, v in sorted(settings.OAUTH.items())],
                                       })(request)


def logout(request):
    return authviews.logout_then_login(request, login_url='/')


def changepwd(request):
    if hasattr(request.user, 'password') and request.user.password == OAUTH_PASSWORD_STORE:
        return HttpSimpleResponse(request, "Account error", "This account cannot change password as it's connected to a third party login site.")

    log.info("Initiating password change from {0}".format(get_client_ip(request)))
    return authviews.PasswordChangeView.as_view(template_name='account/password_change.html',
                                                success_url='/account/changepwd/done/')(request)


def resetpwd(request):
    # Basic django password reset feature is completely broken. For example, it does not support
    # resetting passwords for users with "old hashes", which means they have no way to ever
    # recover. So implement our own, since it's quite the trivial feature.
    if request.method == "POST":
        try:
            u = User.objects.get(email__iexact=request.POST['email'])
            if u.password == OAUTH_PASSWORD_STORE:
                return HttpSimpleResponse(request, "Account error", "This account cannot change password as it's connected to a third party login site.")
        except User.DoesNotExist:
            log.info("Attempting to reset password of {0}, user not found".format(request.POST['email']))
            return HttpResponseRedirect('/account/reset/done/')

        form = PgwebPasswordResetForm(data=request.POST)
        if form.is_valid():
            log.info("Initiating password set from {0} for {1}".format(get_client_ip(request), form.cleaned_data['email']))
            token = default_token_generator.make_token(u)
            send_template_mail(
                settings.ACCOUNTS_NOREPLY_FROM,
                u.email,
                'Password reset for your postgresql.org account',
                'account/password_reset_email.txt',
                {
                    'user': u,
                    'uid': urlsafe_base64_encode(force_bytes(u.pk)),
                    'token': token,
                },
            )
            return HttpResponseRedirect('/account/reset/done/')
    else:
        form = PgwebPasswordResetForm()

    return render_pgweb(request, 'account', 'account/password_reset.html', {
        'form': form,
    })


def change_done(request):
    log.info("Password change done from {0}".format(get_client_ip(request)))
    return authviews.PasswordChangeDoneView.as_view(template_name='account/password_change_done.html')(request)


def reset_done(request):
    log.info("Password reset done from {0}".format(get_client_ip(request)))
    return authviews.PasswordResetDoneView.as_view(template_name='account/password_reset_done.html')(request)


def reset_confirm(request, uidb64, token):
    log.info("Confirming password reset for uidb {0}, token {1} from {2}".format(uidb64, token, get_client_ip(request)))
    return authviews.PasswordResetConfirmView.as_view(template_name='account/password_reset_confirm.html',
                                                      success_url='/account/reset/complete/')(
                                                          request, uidb64=uidb64, token=token)


def reset_complete(request):
    log.info("Password reset completed for user from {0}".format(get_client_ip(request)))
    return authviews.PasswordResetCompleteView.as_view(template_name='account/password_reset_complete.html')(request)


@script_sources('https://www.google.com/recaptcha/')
@script_sources('https://www.gstatic.com/recaptcha/')
@frame_sources('https://www.google.com/')
def signup(request):
    if request.user.is_authenticated:
        return HttpSimpleResponse(request, "Account error", "You must log out before you can sign up for a new account")

    if request.method == 'POST':
        # Attempt to create user then, eh?
        form = SignupForm(get_client_ip(request), data=request.POST)
        if form.is_valid():
            # Attempt to create the user here
            # XXX: Do we need to validate something else?
            log.info("Creating user for {0} from {1}".format(form.cleaned_data['username'], get_client_ip(request)))

            user = User.objects.create_user(form.cleaned_data['username'].lower(), form.cleaned_data['email'].lower(), last_login=datetime.now())
            user.first_name = form.cleaned_data['first_name']
            user.last_name = form.cleaned_data['last_name']

            # generate a random value for password. It won't be possible to log in with it, but
            # it creates more entropy for the token generator (I think).
            user.password = generate_random_token()
            user.save()

            # Now generate a token
            token = default_token_generator.make_token(user)
            log.info("Generated token {0} for user {1} from {2}".format(token, form.cleaned_data['username'], get_client_ip(request)))

            # Generate an outgoing email
            send_template_mail(settings.ACCOUNTS_NOREPLY_FROM,
                               form.cleaned_data['email'],
                               'Your new postgresql.org community account',
                               'account/new_account_email.txt',
                               {'uid': urlsafe_base64_encode(force_bytes(user.id)), 'token': token, 'user': user}
                               )

            return HttpResponseRedirect('/account/signup/complete/')
    else:
        form = SignupForm(get_client_ip(request))

    return render_pgweb(request, 'account', 'base/form.html', {
        'form': form,
        'formitemtype': 'Account',
        'form_intro': """
To sign up for a free community account, enter your preferred userid and email address.
Note that a community account is only needed if you want to submit information - all
content is available for reading without an account. A confirmation email will be sent
to the specified address, and once confirmed a password for the new account can be specified.
""",
        'savebutton': 'Sign up',
        'operation': 'New',
        'recaptcha': True,
    })


def signup_complete(request):
    return render_pgweb(request, 'account', 'account/signup_complete.html', {
    })


@script_sources('https://www.google.com/recaptcha/')
@script_sources('https://www.gstatic.com/recaptcha/')
@frame_sources('https://www.google.com/')
@transaction.atomic
def signup_oauth(request):
    if 'oauth_email' not in request.session \
       or 'oauth_firstname' not in request.session \
       or 'oauth_lastname' not in request.session:
        return HttpSimpleResponse(request, "OAuth error", 'Invalid redirect received')

    if request.method == 'POST':
        # Second stage, so create the account. But verify that the
        # nonce matches.
        data = request.POST.copy()
        data['email'] = request.session['oauth_email'].lower()
        data['first_name'] = request.session['oauth_firstname']
        data['last_name'] = request.session['oauth_lastname']
        form = SignupOauthForm(data=data)
        if form.is_valid():
            log.info("Creating user for {0} from {1} from oauth signin of email {2}".format(form.cleaned_data['username'], get_client_ip(request), request.session['oauth_email']))

            user = User.objects.create_user(form.cleaned_data['username'].lower(),
                                            request.session['oauth_email'].lower(),
                                            last_login=datetime.now())
            user.first_name = request.session['oauth_firstname']
            user.last_name = request.session['oauth_lastname']
            user.password = OAUTH_PASSWORD_STORE
            user.save()

            # Clean up our session
            del request.session['oauth_email']
            del request.session['oauth_firstname']
            del request.session['oauth_lastname']
            request.session.modified = True

            # We can immediately log the user in because their email
            # is confirmed.
            user.backend = settings.AUTHENTICATION_BACKENDS[0]
            django_login(request, user)

            # Redirect to the sessions page, or to the account page
            # if none was given.
            return HttpResponseRedirect(request.session.pop('login_next', '/account/'))
    elif 'do_abort' in request.GET:
        del request.session['oauth_email']
        del request.session['oauth_firstname']
        del request.session['oauth_lastname']
        request.session.modified = True
        return HttpResponseRedirect(request.session.pop('login_next', '/'))
    else:
        # Generate possible new username
        suggested_username = request.session['oauth_email'].replace('@', '.')[:30]

        # Auto generation requires firstname and lastname to be specified
        f = request.session['oauth_firstname'].lower()
        l = request.session['oauth_lastname'].lower()
        if f and l:
            for u in itertools.chain([
                    "{0}{1}".format(f, l[0]),
                    "{0}{1}".format(f[0], l),
            ], ("{0}{1}{2}".format(f, l[0], n) for n in range(100))):
                if not User.objects.filter(username=u[:30]).exists():
                    suggested_username = u[:30]
                    break

        form = SignupOauthForm(initial={
            'username': suggested_username,
            'email': request.session['oauth_email'].lower(),
            'first_name': request.session['oauth_firstname'][:30],
            'last_name': request.session['oauth_lastname'][:30],
        })

    return render_pgweb(request, 'account', 'account/signup_oauth.html', {
        'form': form,
        'operation': 'New account',
        'savebutton': 'Sign up for new account',
        'recaptcha': True,
    })


####
# Community authentication endpoint
####
def communityauth(request, siteid):
    # Get whatever site the user is trying to log in to.
    site = get_object_or_404(CommunityAuthSite, pk=siteid)

    # "suburl" - old style way of passing parameters
    # deprecated - will be removed once all sites have migrated
    if 'su' in request.GET:
        su = request.GET['su']
        if not su.startswith('/'):
            su = None
    else:
        su = None

    # "data" - new style way of passing parameter, where we only
    # care that it's characters are what's in base64.
    if 'd' in request.GET:
        d = request.GET['d']
        if d != urllib.parse.quote_plus(d, '=$'):
            # Invalid character, so drop it
            d = None
    else:
        d = None

    if d:
        urldata = "?d=%s" % d
    elif su:
        urldata = "?su=%s" % su
    else:
        urldata = ""

    # Verify if the user is authenticated, and if he/she is not, generate
    # a login form that has information about which site is being logged
    # in to, and basic information about how the community login system
    # works.
    if not request.user.is_authenticated:
        if request.method == "POST" and 'next' in request.POST and 'this_is_the_login_form' in request.POST:
            # This is a postback of the login form. So pick the next filed
            # from that one, so we keep it across invalid password entries.
            nexturl = request.POST['next']
        else:
            nexturl = '/account/auth/%s/%s' % (siteid, urldata)
        return authviews.LoginView.as_view(
            template_name='account/login.html',
            authentication_form=PgwebAuthenticationForm,
            extra_context={
                'sitename': site.name,
                'next': nexturl,
                'oauth_providers': [(k, v) for k, v in sorted(settings.OAUTH.items())],
            },
        )(request)

    # When we reach this point, the user *has* already been authenticated.
    # The request variable "su" *may* contain a suburl and should in that
    # case be passed along to the site we're authenticating for. And of
    # course, we fill a structure with information about the user.

    if request.user.first_name == '' or request.user.last_name == '' or request.user.email == '':
        return render_pgweb(request, 'account', 'account/communityauth_noinfo.html', {
        })

    # Check for cooloff period
    if site.cooloff_hours > 0:
        if (datetime.now() - request.user.date_joined) < timedelta(hours=site.cooloff_hours):
            log.warning("User {0} tried to log in to {1} before cooloff period ended.".format(
                request.user.username, site.name))
            return render_pgweb(request, 'account', 'account/communityauth_cooloff.html', {
                'site': site,
            })

    if site.org.require_consent:
        if not CommunityAuthConsent.objects.filter(org=site.org, user=request.user).exists():
            return HttpResponseRedirect('/account/auth/{0}/consent/?{1}'.format(siteid,
                                                                                urllib.parse.urlencode({'next': '/account/auth/{0}/{1}'.format(siteid, urldata)})))

    # Record the login as the last login to this site. Django doesn't support tables with
    # multi-column PK, so we have to do this in a raw query.
    with connection.cursor() as curs:
        curs.execute("INSERT INTO account_communityauthlastlogin (user_id, site_id, lastlogin, logincount) VALUES (%(userid)s, %(siteid)s, CURRENT_TIMESTAMP, 1) ON CONFLICT (user_id, site_id) DO UPDATE SET lastlogin=CURRENT_TIMESTAMP, logincount=account_communityauthlastlogin.logincount+1", {
            'userid': request.user.id,
            'siteid': site.id,
        })

    info = {
        'u': request.user.username.encode('utf-8'),
        'f': request.user.first_name.encode('utf-8'),
        'l': request.user.last_name.encode('utf-8'),
        'e': request.user.email.encode('utf-8'),
        'se': ','.join([a.email for a in SecondaryEmail.objects.filter(user=request.user, confirmed=True).order_by('email')]).encode('utf8'),
    }
    if d:
        info['d'] = d.encode('utf-8')
    elif su:
        info['su'] = su.encode('utf-8')

    # Turn this into an URL. Make sure the timestamp is always first, that makes
    # the first block more random..
    s = "t=%s&%s" % (int(time.time()), urllib.parse.urlencode(info))

    # Encrypt it with the shared key (and IV!)
    r = Random.new()
    iv = r.read(16)  # Always 16 bytes for AES
    encryptor = AES.new(base64.b64decode(site.cryptkey), AES.MODE_CBC, iv)
    cipher = encryptor.encrypt(s.encode('ascii') + b' ' * (16 - (len(s) % 16)))  # Pad to even 16 bytes

    # Generate redirect
    return HttpResponseRedirect("%s?i=%s&d=%s" % (
        site.redirecturl,
        base64.b64encode(iv, b"-_").decode('ascii'),
        base64.b64encode(cipher, b"-_").decode('ascii'),
    ))


def communityauth_logout(request, siteid):
    # Get whatever site the user is trying to log in to.
    site = get_object_or_404(CommunityAuthSite, pk=siteid)

    if request.user.is_authenticated:
        django_logout(request)

    # Redirect user back to the specified suburl
    return HttpResponseRedirect("%s?s=logout" % site.redirecturl)


@login_required
def communityauth_consent(request, siteid):
    org = get_object_or_404(CommunityAuthSite, id=siteid).org
    if request.method == 'POST':
        form = CommunityAuthConsentForm(org.orgname, data=request.POST)
        if form.is_valid():
            CommunityAuthConsent.objects.get_or_create(user=request.user, org=org,
                                                       defaults={'consentgiven': datetime.now()},
                                                       )
            return HttpResponseRedirect(form.cleaned_data['next'])
    else:
        form = CommunityAuthConsentForm(org.orgname, initial={'next': request.GET.get('next', '')})

    return render_pgweb(request, 'account', 'base/form.html', {
        'form': form,
        'operation': 'Authentication',
        'form_intro': 'The site you are about to log into is run by {0}. If you choose to proceed with this authentication, your name and email address will be shared with <em>{1}</em>.</p><p>Please confirm that you consent to this sharing.'.format(org.orgname, org.orgname),
        'savebutton': 'Proceed with login',
    })


def _encrypt_site_response(site, s):
    # Encrypt it with the shared key (and IV!)
    r = Random.new()
    iv = r.read(16)  # Always 16 bytes for AES
    encryptor = AES.new(base64.b64decode(site.cryptkey), AES.MODE_CBC, iv)
    cipher = encryptor.encrypt(s.encode('ascii') + b' ' * (16 - (len(s) % 16)))  # Pad to even 16 bytes

    # Base64-encode the response, just to be consistent
    return "%s&%s" % (
        base64.b64encode(iv, b'-_').decode('ascii'),
        base64.b64encode(cipher, b'-_').decode('ascii'),
    )


def communityauth_search(request, siteid):
    # Perform a search for users. The response will be encrypted with the site
    # key to prevent abuse, therefor we need the site.
    site = get_object_or_404(CommunityAuthSite, pk=siteid)

    q = Q(is_active=True)
    if 's' in request.GET and request.GET['s']:
        # General search term, match both name and email
        q = q & (Q(email__icontains=request.GET['s']) | Q(first_name__icontains=request.GET['s']) | Q(last_name__icontains=request.GET['s']))
    elif 'e' in request.GET and request.GET['e']:
        q = q & Q(email__icontains=request.GET['e'])
    elif 'n' in request.GET and request.GET['n']:
        q = q & (Q(first_name__icontains=request.GET['n']) | Q(last_name__icontains=request.GET['n']))
    elif 'u' in request.GET and request.GET['u']:
        q = q & Q(username=request.GET['u'])
    else:
        raise Http404('No search term specified')

    users = User.objects.prefetch_related(Prefetch('secondaryemail_set', queryset=SecondaryEmail.objects.filter(confirmed=True))).filter(q)

    j = json.dumps([{
        'u': u.username,
        'e': u.email,
        'f': u.first_name,
        'l': u.last_name,
        'se': [a.email for a in u.secondaryemail_set.all()],
    } for u in users])

    return HttpResponse(_encrypt_site_response(site, j))


def communityauth_getkeys(request, siteid, since=None):
    # Get any updated ssh keys for community accounts.
    # The response will be encrypted with the site key to prevent abuse,
    # therefor we need the site.
    site = get_object_or_404(CommunityAuthSite, pk=siteid)

    if since:
        keys = UserProfile.objects.select_related('user').filter(lastmodified__gte=datetime.fromtimestamp(int(since.replace('/', '')))).exclude(sshkey='')
    else:
        keys = UserProfile.objects.select_related('user').all().exclude(sshkey='')

    j = json.dumps([{'u': k.user.username, 's': k.sshkey.replace("\r", "\n")} for k in keys])

    return HttpResponse(_encrypt_site_response(site, j))
