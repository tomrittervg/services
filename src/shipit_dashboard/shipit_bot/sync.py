# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from releng_common import log
import mohawk
import requests
import taskcluster
import json

from shipit_bot.helpers import compute_dict_hash, ShipitJSONEncoder
from libmozdata import bugzilla
from libmozdata.patchanalysis import bug_analysis, parse_uplift_comment


logger = log.get_logger('shipit_bot')


class BugSync(object):
    """
    Helper class to sync bugs between
    Bugzilla & remote server
    """
    def __init__(self, bugzilla_id):
        self.bugzilla_id = bugzilla_id
        self.on_remote = []
        self.on_bugzilla = []
        self.bug_data = None
        self.payload = None

    def update(self, bugzilla_url):
        """
        Update bug used in this sync
        """

        # Skip when it's already processed in instance
        if self.payload is not None:
            logger.warn('Bug {} already processed.'.format(self.bugzilla_id))
            return True

        # Compute the hash of the new bug
        bug_hash = compute_dict_hash(self.bug_data)

        # Do patch analysis
        try:
            analysis = bug_analysis(self.bugzilla_id)
        except Exception as e:
            logger.error('Patch analysis failed on {} : {}'.format(self.bugzilla_id, e))  # noqa
            return False

        # Build html version of uplift comment
        if analysis['uplift_comment']:
            analysis['uplift_comment']['html'] = parse_uplift_comment(
                analysis['uplift_comment']['text'], self.bugzilla_id)

        # Build internal payload
        self.payload = {
            'bugzilla_id': self.bugzilla_id,
            'analysis': self.on_bugzilla,
            'payload': {
                'url': '{}/{}'.format(bugzilla_url, self.bugzilla_id),
                'bug': self.bug_data,
                'analysis': analysis,
                'users': self.load_users(analysis),
            },
            'payload_hash': bug_hash,
        }
        logger.info('Updated payload of {}'.format(self.bugzilla_id))

        return True

    def load_users(self, analysis):
        """
        Load users linked through roles to an analysis
        """
        roles = {}

        def _extract_user(user_data, role):
            # Support multiple input structures
            if user_data is None:
                return
            elif isinstance(user_data, dict):
                if 'id' in user_data:
                    key = user_data['id']
                elif 'email' in user_data:
                    key = user_data['email']
                else:
                    raise Exception('Invalid user data : no id or email')

            elif isinstance(user_data, str):
                key = user_data
            else:
                raise Exception('Invalid user data : unsupported format')

            if key not in roles:
                roles[key] = []
            roles[key].append(role)

        # Extract users keys & roles
        _extract_user(analysis['users'].get('creator'), 'creator')
        _extract_user(analysis['users'].get('assignee'), 'assignee')
        for r in analysis['users']['reviewers']:
            _extract_user(r, 'reviewer')
        _extract_user(analysis['uplift_author'], 'uplift_author')

        def _handler(user, data):
            # Store users with their roles
            user['roles'] = roles.get(user['id'], roles.get(user['email'], []))
            data.append(user)

        # Finally fetch clean users data through Bugzilla
        out = []
        bugzilla.BugzillaUser(user_names=roles.keys(),
                              user_handler=_handler,
                              user_data=out).wait()
        return out


class Bot(object):
    """
    Update all analysis data
    """
    def __init__(self, bugzilla_url, bugzilla_token=None):
        self.bugs = {}
        self.bugzilla_url = bugzilla_url

        # Patch libmozdata configuration
        # TODO: Fix config calls in libmozdata
        # os.environ['LIBMOZDATA_CFG_BUGZILLA_URL'] = self.bugzilla_url
        # set_config(ConfigEnv())
        bugzilla.Bugzilla.URL = self.bugzilla_url
        bugzilla.Bugzilla.API_URL = self.bugzilla_url + '/rest/bug'
        bugzilla.BugzillaUser.URL = self.bugzilla_url
        bugzilla.BugzillaUser.API_URL = self.bugzilla_url + '/rest/user'
        if bugzilla_token is not None:
            bugzilla.Bugzilla.TOKEN = bugzilla_token
            bugzilla.BugzillaUser.TOKEN = bugzilla_token

        logger.info('Use bugzilla server {}'.format(self.bugzilla_url))

    def run(self):
        raise NotImplementedError

    def list_bugs(self, query):
        """
        List all the bugs from a Bugzilla query
        """
        def _bughandler(bug, data):
            bugid = bug['id']
            data[bugid] = bug

        def _attachmenthandler(attachments, bugid, data):
            data[int(bugid)] = attachments

        bugs, attachments = {}, {}

        bz = bugzilla.Bugzilla(query,
                               bughandler=_bughandler,
                               attachmenthandler=_attachmenthandler,
                               bugdata=bugs,
                               attachmentdata=attachments)
        bz.get_data().wait()

        # Map attachments on bugs
        for bugid, _attachments in attachments.items():
            if bugid not in bugs:
                continue
            bugs[bugid]['attachments'] = _attachments

        return bugs


class BotRemote(Bot):
    """
    Use a distant shipit api server
    to store processed analysis
    """
    def __init__(self, secrets_path, client_id=None, access_token=None):
        # Start by loading secrets from Taskcluster
        secrets = self.load_secrets(secrets_path, client_id, access_token)

        super(BotRemote, self).__init__(
            secrets['bugzilla_url'],
            secrets['bugzilla_token']
        )
        self.api_url = secrets['api_url']
        self.sync = {}  # init

    def load_secrets(self, secrets_path, client_id=None, access_token=None):
        """
        Load Taskcluster secrets
        """

        # Setup credentials for Shipit api
        self.credentials = {
          'id': client_id,
          'key': access_token,
          'algorithm': 'sha256',
        }

        if client_id and access_token:
            # Use provided credentials
            tc = taskcluster.Secrets({
                'credentials': {
                    'clientId': client_id,
                    'accessToken': access_token,
                }
            })

        else:
            # Load secrets from TC task context
            # with taskclusterProxy
            tc = taskcluster.Secrets({
                'baseUrl': 'http://taskcluster/secrets/v1'
            })

        # Check mandatory keys in secrets
        secrets = tc.get(secrets_path)
        secrets = secrets['secret']
        required = ('bugzilla_url', 'bugzilla_token', 'api_url')
        for req in required:
            if req not in secrets:
                raise Exception('Missing value {} in Taskcluster secret value {}'.format(req, secrets_path))  # noqa

        return secrets

    def make_request(self, method, url, data=''):
        """
        Make an HAWK authenticated request on remote server
        """
        request = getattr(requests, method)
        if not request:
            raise Exception('Invalid method {}'.format(method))

        # Build HAWK token
        url = self.api_url + url
        hawk = mohawk.Sender(self.credentials,
                             url,
                             method,
                             content=data,
                             content_type='application/json')

        # Send request
        headers = {
            'Authorization': hawk.request_header,
            'Content-Type': 'application/json',
        }
        response = request(url, data=data, headers=headers, verify=False)
        if not response.ok:
            raise Exception('Invalid response from {} {} : {}'.format(
                method, url, response.content))

        return response.json()

    def get_bug_sync(self, bugzilla_id):
        if bugzilla_id not in self.sync:
            # Init new bug sync
            bug = BugSync(bugzilla_id)
            self.sync[bugzilla_id] = bug

        return self.sync[bugzilla_id]

    def run(self):
        """
        Build bug analysis for a specified Bugzilla query
        Used by taskcluster - no db interaction
        """

        # Load all analysis
        all_analysis = self.make_request('get', '/analysis')
        for analysis in all_analysis:

            # Mark bugs already in analysis
            logger.info('List remote bugs for {}'.format(analysis['name']))
            analysis_details = self.make_request('get', '/analysis/{}'.format(analysis['id']))  # noqa
            syncs = map(self.get_bug_sync, [b['bugzilla_id'] for b in analysis_details['bugs']])  # noqa
            for sync in syncs:
                sync.on_remote.append(analysis['id'])

            # Get bugs from bugzilla for this analysis
            logger.info('List bugzilla bugs for {}'.format(analysis['name']))
            raw_bugs = self.list_bugs(analysis['parameters'])
            for bugzilla_id, bug_data in raw_bugs.items():
                sync = self.get_bug_sync(bugzilla_id)
                if sync.bug_data is None:
                    sync.bug_data = bug_data
                sync.on_bugzilla.append(analysis['id'])

        for bugzilla_id, sync in self.sync.items():

            if len(sync.on_bugzilla) > 0:
                # Do patch analysis on bugs
                if not sync.update(self.bugzilla_url):
                    continue

                # Send payload to server
                try:
                    data = json.dumps(sync.payload, cls=ShipitJSONEncoder)
                    self.make_request('post', '/bugs', data)
                    logger.info('Added bug #{} on analysis {}'.format(
                        bugzilla_id, ', '.join(map(str, sync.on_bugzilla))))
                except Exception as e:
                    logger.error('Failed to add bug #{} : {}'.format(bugzilla_id, e))  # noqa

            elif len(sync.on_remote) > 0:
                # Remove bugs from remote server
                try:
                    self.make_request('delete', '/bugs/{}'.format(bugzilla_id))
                    logger.info('Deleted bug #{} from analysis {}'.format(
                        bugzilla_id, ', '.join(map(str, sync.on_remote))))
                except Exception as e:
                    logger.warning(
                        'Failed to delete bug #{} : {}'.format(bugzilla_id, e))