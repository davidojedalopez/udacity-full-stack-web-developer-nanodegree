#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import Session
from models import SessionForm
from models import SessionForms
from models import SpeakerForm
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import BooleanMessage
from models import ConflictException
from models import StringMessage

from utils import getUserId

from settings import WEB_CLIENT_ID

from google.appengine.api import memcache

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
    'EQ':   '=',
    'GT':   '>',
    'GTEQ': '>=',
    'LT':   '<',
    'LTEQ': '<=',
    'NE':   '!='
}

FIELDS = {
    'CITY': 'city',
    'TOPIC': 'topics',
    'MONTH': 'month',
    'MAX_ATTENDEES': 'maxAttendees',
}

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESSION_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1, required=True),
    typeOfSession=messages.StringField(2)
)

SESSION_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1, required=True),
)

WISHLIST_POST_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1, required=True),
)

SPEAKER_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speaker=messages.StringField(1, required=True),
)

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "Last chance to attend!"

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api( name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf

    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()
        
        # send email to organizer confirming     
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )

        return request

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q

    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)    

    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
        path='conference/{websafeConferenceKey}',
        http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)

    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
        path='conference/{websafeConferenceKey}',
        http_method='GET', 
        name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(ConferenceForm, ConferenceForm,
        path='conference',
        http_method='POST',
        name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    @endpoints.method(ConferenceQueryForms, ConferenceForms,
        path='queryConferences',
        http_method='POST',
        name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

         # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
            for conf in conferences]
        )

    # @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
    #     path='conferences/by_month/{month}',
    #     http_method='GET',
    #     name='getConferenceByMonth')
    # def getConferenceByMonth(self, request):
    #     """Given a month, return all conferences taking place on that month"""
    #     # Copy ConferenceForm/ProtoRPC Message into dict
    #     conferences = self._getQuery(request)

    #     return ConferenceForms(
    #         items=[self._copyConferenceToForm(conference) for conference in conferences]
    #         )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='getConferencesCreated',
        http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # make profile key
        p_key = ndb.Key(Profile, getUserId(user))
        # create ancestor query for this user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName) for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='filterPlayground',
        http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        q = Conference.query()
        # simple filter usage:
        # q = q.filter(Conference.city == "Paris")

        # advanced filter building and usage    
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)

        # TODO
        # add 2 filters:
        # 1: city equals to London
        field = "city"
        operator = "="
        value = "London"
        f = ndb.query.FilterNode(field, operator, value)
        q = q.filter(f)

        # 2: topic equals "Medical Innovations"
        field = "topics"
        operator = "="
        value = "Medical Innovations"
        f2 = ndb.query.FilterNode(field, operator, value)
        q = q.filter(f2)

        # 3: Order by name
        q = q.order(Conference.name)

        # 4: Filter for june
        q = q.filter(Conference.maxAttendees > 10)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

    @endpoints.method(CONF_GET_REQUEST, SessionForms,
        path='conference/{websafeConferenceKey}/sessions',
        http_method='GET',
        name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Given a conference, return all its sessions"""

        # Copy ConferenceForm/ProtpRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # Get existing conference
        conference = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conference:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # Create ancestor query for all key matches for this conference
        sessions = Session.query(ancestor=ndb.Key(Conference, conference.key.id()))

        # Return set of SessionForm objects for each Session
        return SessionForms(
            items = [self._copySessionToForm(session) for session in sessions])

    @endpoints.method(SESSION_GET_REQUEST, SessionForms,
        path='conference/{websafeConferenceKey}/sessions/by_type/{typeOfSession}',
        http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Given a conference, return all sessions of a specified type"""

        # Copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        typeOfSession = data['typeOfSession']

        # Get existing conference
        conference = ndb.Key(urlsafe=request.websafeConferenceKey).get()    
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # Create ancestor query for all key matches for this conference
        sessions = Session.query(Session.typeOfSession == typeOfSession, ancestor=ndb.Key(Conference, conf.key.id()))

        # Return set of SessionForm objects for each Session
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
        path='conference/{websafeConferenceKey}',
        http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
        path='conference/{websafeConferenceKey}',
        http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference"""
        return self._conferenceRegistration(request, reg=False)

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='conferences/attending',
        http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        # TODO:
        # step 1: get user profile
        prof = self._getProfileFromUser()

        # step 2: get conferenceKeysToAttend from profile.
        # to make a ndb key from websafe key you can use:
        # ndb.Key(urlsafe=my_websafe_key_string)
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName        

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, "")\
         for conf in conferences]
        )

# - - - Announcements - - - - - - - - - - - - - - - - - - - -

# TODO 1
    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement

    @endpoints.method(message_types.VoidMessage, StringMessage,
        path='conference/announcement/get',
        http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        # TODO 1
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY)
        if not announcement:
            announcement = ""
        return StringMessage(data=announcement)


# - - - Sessions - - - - - - - - - - - - - - - - - - - -
    
    def _createSessionObject(self, request):
        """Create or update Session object, returning SessionForm/request."""
        # Preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException('Sesssion "name" field required')

        # Get and check for conference
        conference = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conference:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'\
                % request.websafeConferenceKey)

        # Check that user is organizer
        if user_id != conference.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the organizer can add sessions.')

        # Copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # Do I need to parse Time object?

        # Add default values to session

        # Convert dates from strings to Date objects; set month based on start_date
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()            
        
        # Convert time from strings to Time object (date-independent)
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()

        # Generate parent (Conference) Key
        parent_key = ndb.Key(Conference, conference.key.id())
        # Allocate new child (Sesssion) ID with parent_key as parent
        child_id = Session.allocate_ids(size=1, parent=parent_key)[0]
        # Make child Key from ID
        child_key = ndb.Key(Session, child_id, parent=parent_key)
        data['key'] = child_key
        data['organizerUserId'] = user_id
        del data['websafeConferenceKey']
        del data['websafeKey']

        Session(**data).put()

        # Check if speaker exists in other sections, if so, add to memcache
        sessions = Session.query(Session.speaker == data['speaker'],
            ancestor=parent_key)
        if len(list(sessions)) > 1:
            cache_data = {}
            cache_data['speaker'] = data['speaker']
            cache_data['sessionNames'] = [session.name for session in sessions]
            if not memcache.set('featured_speaker', cache_data):
                logging.error('Memcache set failed.')        

        return request

    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm"""
        session_form = SessionForm()
        for field in session_form.all_fields():
            if hasattr(session, field.name):
                if field.name in ['startTime', 'date']:
                    setattr(session_form, field.name, str(getattr(session, field.name)))
                else:
                    setattr(session_form, field.name, getattr(session, field.name))
            elif field.name == 'websafeKey':
                setattr(session_form, field.name, session.key.urlsafe())
        session_form.check_initialized()
        return session_form

    @endpoints.method(WISHLIST_POST_REQUEST, SessionForm,
        http_method='POST',
        name='addSessionToWishList')
    def addSessionToWishList(self, request):
        """Saves a session to a user's wishlist"""
        user = endpoints.get_current_user()
        if not user:
            raise UnauthorizedException('Authorization required')

        # Check that the session exists
        session = ndb.Key(urlsafe=request.websafeSessionKey).get()
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key %s' % request.websafeSessionKey)

        # Get Profile
        profile = self._getProfileFromUser()

        # Check if session is already in wishlist
        if session.key in profile.sessionsToAttend:
            raise endpoints.BadRequestException(
                'Session %s is already saved to wishlist' % request.websafeSessionKey)

        # Append to user's profile wishlist
        profile.sessionsToAttend.append(session.key)
        profile.put()

        return self._copySessionToForm(session)

    @endpoints.method(message_types.VoidMessage, SessionForms,
        http_method='POST',
        name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Returns a user's wishlist of sessions"""
        user = endpoints.get_current_user()
        if not user:
            raise UnauthorizedException('Authorization required')

        profile = self._getProfileFromUser()

        sessions_keys = profile.sessionsToAttend
        sessions = ndb.get_multi(sessions_keys)

        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions])

    @endpoints.method(WISHLIST_POST_REQUEST, SessionForm,
        http_method='POST',
        name='deleteSessionInWishlist')
    def deleteSessionInWishlist(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        profile = self._getProfileFromUser()

        session = ndb.Key(urlsafe=request.websafeSessionKey).get()       
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key %s' % request.websafeSessionKey)

        # Check if session is already in wishlist
        if session.key not in profile.sessionsToAttend:
            raise endpoints.BadRequestException(
                'Session %s is not in the wishlist' % request.websafeSessionKey)

        profile.sessionsToAttend.remove(session.key)
        profile.put()

        return self._copySessionToForm(session)

    @endpoints.method(SessionForm, SessionForm,
        path='sessions',
        http_method='POST',
        name='createSession')
    def createSession(self, request):
        """Create a session. Open to the organizer of the conference only"""
        return self._createSessionObject(request)

    @endpoints.method(SPEAKER_GET_REQUEST, SessionForms,
            path='sessions/{speaker}',
            http_method='GET',
            name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Given a speaker, return all sessions given by him/her across all conferences"""
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        speaker = data['speaker']

        # query sessions by speaker, exact match
        sessions = Session.query(Session.speaker == speaker)

        # return set of ConferenceForm objects per Conference
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(message_types.VoidMessage, SpeakerForm,
        http_method='GET',
        name='getFeaturedSpeakerSessions')
    def getFeaturedSpeakerSessions(self, request):
        """Returns the sessions of the given speaker"""
        # Try to get data from memcache
        data = memcache.get('featured_speaker')
        sessions = []
        sessionNames = []
        speaker = None

        if data and data.has_key('speaker') and data.has_key('sessionNames'):
            speaker = data['speaker']
            sessionNames = data['sessionNames']

        # If data is not on memcache, get speaker from upcoming session
        else:
            nextSession = Session.query(Session.date >= datetime.now()).order(Session.date, Session.startTime).get()
            if nextSession:
                speaker = nextSession.speaker
                sessions = Session.query(Session.speaker == speaker)
                sessionNames = [session.name for session in sessions]

        # Fill speaker form
        speaker_form = SpeakerForm()
        for field in speaker_form.all_fields():
            if field.name ==  'sessionNames':
                setattr(speaker_form, field.name, sessionNames)
            elif field.name == 'speaker':
                setattr(speaker_form, field.name, speaker)
        speaker_form.check_initialized()
        return speaker_form

# registers API
api = endpoints.api_server([ConferenceApi]) 
