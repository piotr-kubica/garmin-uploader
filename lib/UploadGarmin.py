"""
Upload Garmin 

Handle the operation to upload to the Garmin Connect Website.

"""
# 
# 
# This version of UploadGarmin.py leverages heavily from work done in the 
# tapiriik project (https://github.com/cpfair/tapiriik), particularly the new
# Garmin Connect user authentication using Jasig CAS.
#
# Copyright (c) David Lotton 02/2014
#
# License: Apache 2.0
#
# Information: 2/26/2014
# Complete redesign of UploadGarmin.py due to major changes in the Garmin 
# Connect authorization scheme which rolled out in late Feb 2014. License has 
# change from previous version of this file to comply with licence of the work
# from which this work was derived.
#
# THE COPYRIGHT HOLDERS AND/OR OTHER PARTIES PROVIDE THE PROGRAM 
# 'AS IS' WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED OR 
# IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES 
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. THE 
# ENTIRE RISK AS TO THE QUALITY AND PERFORMANCE OF THE PROGRAM 
# IS WITH YOU. SHOULD THE PROGRAM PROVE DEFECTIVE, YOU ASSUME 
# THE COST OF ALL NECESSARY SERVICING, REPAIR OR CORRECTION.
#

import requests
import time
import re
import logging
from urllib import urlencode

try:
    import simplejson
except ImportError:
    import json as simplejson
import os.path

VALID_GARMIN_FILE_EXTENSIONS = ('.tcx', '.fit', '.gpx')
BINARY_FILE_FORMATS = ('.fit',)

activityTypes = ('running', 'cycling', 'mountain_biking', 'walking', 'hiking',
                 'resort_skiing_snowboarding', 'cross_country_skiing', 
                 'skating', 'swimming', 'rowing', 'elliptical', 
                 'fitness_equipment', 'other')

class ServiceExceptionScope:
    Account = "account"
    Service = "service"

class ServiceException(Exception):
    def __init__(self, message, scope=ServiceExceptionScope.Service, block=False, user_exception=None):
        Exception.__init__(self, message)
        self.Message = message
        self.UserException = user_exception
        self.Block = block
        self.Scope = scope

    def __str__(self):
        return self.Message + " (user " + str(self.UserException) + " )"

class APIException(ServiceException):
    pass

class UserExceptionType:
    # Account-level exceptions (not a hardcoded thing, just to keep these seperate)
    Authorization = "auth"
    AccountFull = "full"
    AccountExpired = "expired"
    AccountUnpaid = "unpaid" # vs. expired, which implies it was at some point function, via payment or trial or otherwise.

    # Activity-level exceptions
    FlowException = "flow"
    Private = "private"
    NotTriggered = "notrigger"
    MissingCredentials = "credentials_missing" # They forgot to check the "Remember these details" box
    NotConfigured = "config_missing" # Don't think this error is even possible any more.
    StationaryUnsupported = "stationary"
    TypeUnsupported = "type_unsupported"
    DownloadError = "download"
    ListingError = "list" # Cases when a service fails listing, so nothing can be uploaded to it.
    UploadError = "upload"
    SanityError = "sanity"
    Corrupt = "corrupt" # Kind of a scary term for what's generally "some data is missing"
    Untagged = "untagged"
    LiveTracking = "live"
    UnknownTZ = "tz_unknown"
    System = "system"
    Other = "other"

class UserException:
    def __init__(self, type, extra=None, intervention_required=False, clear_group=None):
        self.Type = type
        self.Extra = extra # Unimplemented - displayed as part of the error message.
        self.InterventionRequired = intervention_required # Does the user need to dismiss this error?
        self.ClearGroup = clear_group if clear_group else type # Used to group error messages displayed to the user, and let them clear a group that share a common cause.


class UploadGarmin:
    """
    Upload Garmin

    Handle operation to open to Garmin
    """
    def __init__(self, logLevel = 30):
        self.rawHierarchy = requests.get("http://connect.garmin.com/proxy/activity-service-1.2/json/activity_types").text
        self.activityHierarchy = simplejson.loads(self.rawHierarchy)["dictionary"]
        self._last_req_start = None
        self.cookies = None
        self.msgLogger = logging.getLogger(__name__)
        self.msgLogger.setLevel(level=logLevel)
        self.ch = logging.StreamHandler()
        self.ch.setLevel(level=logLevel)
        self.formatter = logging.Formatter('%(asctime)s::%(name)s::%(levelname)s::%(message)s')
        self.ch.setFormatter(self.formatter)
        self.msgLogger.addHandler(self.ch)
        #logging.basicConfig(level=logLevel)


    def _rate_limit(self):
        min_period = 1 # I appear to been banned from Garmin Connect while determining this.
        if not self._last_req_start:
            self._last_req_start = 0.0
        
        wait_time = max(0, min_period - (time.time() - self._last_req_start))
        time.sleep(wait_time)
        
        self._last_req_start = time.time()
        self.msgLogger.info("Rate limited for %f" % wait_time)


    def login(self, username, password):
        if self._get_cookies(username=username, password=password):
            return True
        else:
            return False


    def _get_cookies(self, username=None, password=None):
        if self.cookies:
            return self.cookies

        if username is None or password is None:
            raise APIException("Username and/or password missing")

        self._rate_limit()
        gcPreResp = requests.get("http://connect.garmin.com/", allow_redirects=False)
        # New site gets this redirect, old one does not
        if gcPreResp.status_code == 200:
            self._rate_limit()
            gcPreResp = requests.get("https://connect.garmin.com/signin", allow_redirects=False)
            req_count = int(re.search("j_id(\d+)", gcPreResp.text).groups(1)[0])
            params = {"login": "login", "login:loginUsernameField": username, "login:password": password, "login:signInButton": "Sign In"}
            auth_retries = 3 # Did I mention Garmin Connect is silly?
            for retries in range(auth_retries):
                params["javax.faces.ViewState"] = "j_id%d" % req_count
                req_count += 1
                self._rate_limit()
                resp = requests.post("https://connect.garmin.com/signin", data=params, allow_redirects=False, cookies=gcPreResp.cookies)
                if resp.status_code >= 500 and resp.status_code < 600:
                    raise APIException("Remote API failure")
                if resp.status_code != 302: # yep
                    if "errorMessage" in resp.text:
                        if retries < auth_retries - 1:
                            time.sleep(1)
                            continue
                        else:
                            raise APIException("Invalid login", block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
                    else:
                        raise APIException("Mystery login error %s" % resp.text)
                break
        elif gcPreResp.status_code == 302:
            # JSIG CAS, cool I guess.
            # Not quite OAuth though, so I'll continue to collect raw credentials.
            # Commented stuff left in case this ever breaks because of missing parameters...
            data = {
                "username": username,
                "password": password,
                "_eventId": "submit",
                "embed": "true",
                # "displayNameRequired": "false"
            }
            params = {
                "service": "http://connect.garmin.com/post-auth/login",
                # "redirectAfterAccountLoginUrl": "http://connect.garmin.com/post-auth/login",
                # "redirectAfterAccountCreationUrl": "http://connect.garmin.com/post-auth/login",
                # "webhost": "olaxpw-connect00.garmin.com",
                "clientId": "GarminConnect",
                # "gauthHost": "https://sso.garmin.com/sso",
                # "rememberMeShown": "true",
                # "rememberMeChecked": "false",
                "consumeServiceTicket": "false",
                # "id": "gauth-widget",
                # "embedWidget": "false",
                # "cssUrl": "https://static.garmincdn.com/com.garmin.connect/ui/src-css/gauth-custom.css",
                # "source": "http://connect.garmin.com/en-US/signin",
                # "createAccountShown": "true",
                # "openCreateAccount": "false",
                # "usernameShown": "true",
                # "displayNameShown": "false",
                # "initialFocus": "true",
                # "locale": "en"
            }
            # I may never understand what motivates people to mangle a perfectly good protocol like HTTP in the ways they do...
            preResp = requests.get("https://sso.garmin.com/sso/login", params=params)
            if preResp.status_code != 200:
                raise APIException("SSO prestart error %s %s" % (preResp.status_code, preResp.text))
            data["lt"] = re.search("name=\"lt\"\s+value=\"([^\"]+)\"", preResp.text).groups(1)[0]

            ssoResp = requests.post("https://sso.garmin.com/sso/login", params=params, data=data, allow_redirects=False, cookies=preResp.cookies)
            if ssoResp.status_code != 200:
                raise APIException("SSO error %s %s" % (ssoResp.status_code, ssoResp.text))

            ticket_match = re.search("ticket=([^']+)'", ssoResp.text)
            if not ticket_match:
                raise APIException("Invalid login", block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
            ticket = ticket_match.groups(1)[0]

            # ...AND WE'RE NOT DONE YET!

            self._rate_limit()
            gcRedeemResp1 = requests.get("http://connect.garmin.com/post-auth/login", params={"ticket": ticket}, allow_redirects=False, cookies=gcPreResp.cookies)
            if gcRedeemResp1.status_code != 302:
                raise APIException("GC redeem 1 error %s %s" % (gcRedeemResp1.status_code, gcRedeemResp1.text))

            self._rate_limit()
            gcRedeemResp2 = requests.get(gcRedeemResp1.headers["location"], cookies=gcPreResp.cookies, allow_redirects=False)
            if gcRedeemResp2.status_code != 302:
                raise APIException("GC redeem 2 error %s %s" % (gcRedeemResp2.status_code, gcRedeemResp2.text))

        else:
            raise APIException("Unknown GC prestart response %s %s" % (gcPreResp.status_code, gcPreResp.text))

        self.cookies = gcPreResp.cookies
        return self.cookies


    def upload_file(self, uploadFile):
        
        extension = os.path.splitext(uploadFile)[1].lower()

        # Valid File extensions are .tcx, .fit, and .gpx
        if extension not in VALID_GARMIN_FILE_EXTENSIONS:
            raise Exception("Invalid File Extension")

        if extension in BINARY_FILE_FORMATS:
            mode = 'rb'
        else:
            mode = 'r'

        # Garmin Connect web site does not comply with RFC 2231.
        # urllib3 (used by the requests module) automatically detects non-ascii
        # characters in filenames and generates the filename* header parameter
        # (with asterisk - signifying that the filename has non-ascii characters)
        # instead of the filename (without asterisk) header parameter.  Garmin
        # Connect does not accept the asterisked version of filename and there
        # is no way to tell urllib3 to not generate it.  The work-around for 
        # Garmin's noncompliant behavior (sending non-ascii characters with the
        # non-asterisked filename parameter) is to always send an ascii encodable 
        # filename.  This is achieved by parsing out the non-ascii characters.
        try:
          uploadFileName = uploadFile.encode('ascii')
        except UnicodeEncodeError:
          uploadFileName = uploadFile.decode('ascii', 'ignore')

        files = {"data": (uploadFileName, open(uploadFile, mode))}
        cookies = self._get_cookies()
        self._rate_limit()
        res = requests.post("http://connect.garmin.com/proxy/upload-service-1.1/json/upload/%s" % extension, files=files, cookies=cookies)
        res = res.json()["detailedImportResult"]

        if len(res["successes"]) == 0:
            if res["failures"][0]["messages"][0]['code'] == 202:
                return ['EXISTS', res["failures"][0]["internalId"]]
            else:
                return ['FAIL', res["failures"][0]["messages"]]
        else:
            # Upload was successsful
            return ['SUCCESS', res["successes"][0]["internalId"]]

    def set_workout_name(self, workout_id, workout_name):
        encoding_headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"} # GC really, really needs this part, otherwise it throws obscure errors like "Invalid signature for signature method HMAC-SHA1"
        cookies = self._get_cookies()
        #data = {"value": workout_name}
        data=urlencode({"value": workout_name}).encode("UTF-8")
        self._rate_limit()
        res = requests.post('http://connect.garmin.com/proxy/activity-service-1.0/json/name/%d' % (workout_id), data=data, cookies=cookies, headers=encoding_headers)

        if res.status_code == 200:
            res = res.json()["display"]["value"]
            if res == workout_name:
                self.msgLogger.info("Workout name set: %s" % workout_name)
                return True
            else:
                self.msgLogger.error('Workout name not set: %s' % res)
                return False
        else:
            self.msgLogger.error('Workout name not set')
            return False

    # This for API backward compatability
    def name_workout(self, workout_id, workout_name):
        self.msgLogger.warning('name_workout method deprecated. Use set_workout_name instead.')
        return self.set_workout_name(workout_id, workout_name)


    def _check_activity_type(self, activity_type):
        ''' Fetch valid activity types from Garmin Connect,  compare the given
            activity_type against the 'key' and 'display' values in the dictionary
            of valid activities provided by the GC web site.  Returns the 'key'
            which is used to 
        '''
        for activity in self.activityHierarchy:
            if activity_type.lower() in (activity['key'], activity['display'].lower()):
                self.msgLogger.info('Activity type found.  Using \'%s\' activity key.' % activity['key'])
                return activity['key']
        self.msgLogger.error("Activity type not found")
        return False

    def set_activity_type(self, workout_id, activity_type):
        activity_key = self._check_activity_type(activity_type)
        if activity_key is None:
            self.msgLogger.error("Activity type \'%s\' not valid" % activity_type)
            return False
        
        cookies = self._get_cookies()
        #data = {"value": activity_type.encode("UTF-8")}
        self._rate_limit()
        res = requests.post("http://connect.garmin.com/proxy/activity-service-1.2/json/type/" + str(workout_id), data={"value": activity_key}, cookies=cookies)
        
        if res.status_code == 200:
            res = res.json()
            if "activityType" not in res or res["activityType"]["key"] != activity_key:
                self.msgLogger.error("Activity type not set")
                return False
            else:
                self.msgLogger.info("Activity type set")
                return True  
        else:
            return False


if __name__ == '__main__':
    g = UploadGarmin()
    g.login("username", "password")
    wId = g.upload_tcx('/tmp/a.tcx')
    wInfo = g.upload_file('/tmp/a.tcx')
    g.name_workout(wId, "TestWorkout")