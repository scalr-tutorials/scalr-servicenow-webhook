#!/usr/bin/env python

from flask import Flask
from flask import request
from flask import abort

import pytz
import string
import random
import json
import logging
import binascii
import dateutil.parser
import hmac
import os
import requests

from requests.exceptions import ConnectionError
from hashlib import sha1
from datetime import datetime


logging.basicConfig(level=logging.DEBUG)
app = Flask(__name__)

# Configuration variables
SCALR_SIGNING_KEY = os.getenv('SCALR_SIGNING_KEY', '')
SNOW_URL = os.getenv('SNOW_URL', '')
SNOW_USER = os.getenv('SNOW_USER', '')
SNOW_PASS = os.getenv('SNOW_PASS', '')
SCALR_URL = os.getenv('SCALR_URL', '')
SCALR_TABLE = os.getenv('SCALR_TABLE', 'u_scalr_servers')

for var in ['SCALR_SIGNING_KEY', 'SNOW_URL', 'SNOW_USER', 'SNOW_PASS', 'SCALR_URL']:
    logging.info('Config: %s = %s', var, globals()[var] if 'PASS' not in var else '*' * len(globals()[var]))


@app.route("/servicenow/", methods=['POST'])
def webhook_listener():
    if not validate_request(request):
        abort(403)

    data = json.loads(request.data)
    if 'eventName' not in data or 'data' not in data:
        logging.info('Invalid request received')
        abort(404)

    event = data['eventName']
    if event in ['BeforeInstanceLaunch', 'HostInit', 'BeforeHostUp', 'HostUp', 'BeforeHostTerminate',
                 'HostDown', 'IPAddressChanged', 'ResumeComplete', 'HostInitFailed', 'ServiceNowEvent']:
        return update_host(data['data'], event)
    else:
        logging.info('Received request for unhandled event %s', event)
        return ''


def update_host(data, event):
    snow_client = requests.Session()
    snow_client.auth = (SNOW_USER, SNOW_PASS)
    farm_sys_id = data['SCALR_FARM_ID']
    update_server(snow_client, data, farm_sys_id, event)
    return 'Ok'


def update_server(client, data, farm_sys_id, event):
    server_id = data['SCALR_SERVER_ID']
    status = status_from_event(event)

    if event == "HostDown":
        suspended = data['SCALR_IS_SUSPEND']
        if suspended == "1":
            status = "suspended"

    server = snow_get_server_by_id(client, server_id)
    if not server:
        logging.info('Creating a server record in ServiceNow for %s', server_id)
        server = snow_create_server(client, data, farm_sys_id, status)
    else:
        logging.info('Updating server record in ServiceNow for %s', server_id)
        server = snow_update_server(client, server, data, farm_sys_id, status)

def status_from_event(event):
    return {
        'BeforeInstanceLaunch': 'provisioning',
        'HostInit': 'initializing',
        'BeforeHostUp': 'configuring',
        'HostUp': 'running',
        'BeforeHostTerminate': 'deprovisioning',
        'HostDown': 'terminated',
        'ResumeComplete': 'running',
        'HostInitFailed': 'failed',
        'ServiceNowEvent': 'running'
    }.get(event, '')


def server_object(data, farm_sys_id):
    return {
        'u_id': data['SCALR_SERVER_ID'],
        'u_environment_id': data['SCALR_ENV_ID'],
        'u_account_id': data['SCALR_ACCOUNT_ID'],
        'u_cloud_platform': data['SCALR_CLOUD_PLATFORM'],
        'u_cloud_location': data['SCALR_CLOUD_LOCATION'],
        'u_farm_role_alias': data['SCALR_FARM_ROLE_ALIAS'],
        'u_farm_role_id': data['SCALR_FARM_ROLE_ID'],
        'u_hostname': data['SCALR_SERVER_HOSTNAME'],
        'u_public_ip': data['SCALR_EXTERNAL_IP'],
        'u_private_ip': data['SCALR_INTERNAL_IP'],
        'u_instance_type': data['SCALR_SERVER_TYPE'],
        'u_farm': data['SCALR_FARM_NAME']
    }


def snow_get_server_by_id(client, server_id):
    r = client.get(SNOW_URL + 'api/now/table/' + SCALR_TABLE + '?u_id={}'.format(server_id))
    response = r.json()['result']
    if len(response) == 0:
        return None
    if len(response) > 1:
        logging.warning('Warning: several server records found in ServiceNow with id %s', server_id)
    return response[0]


def snow_create_server(client, data, farm_sys_id, status):
    body = server_object(data, farm_sys_id)
    body['u_status'] = status

    r = client.post(SNOW_URL + 'api/now/table/' + SCALR_TABLE, json=body)
    return r.json()['result']


def snow_update_server(client, server, data, farm_sys_id, status):
    body = server_object(data, farm_sys_id)
    # ID never changes, don't update it
    del body['u_id']
    # Update status only if we have a status, which is not always the case
    # See for instance IPAddressChanged events
    if status:
        body['u_status'] = status

    r = client.patch(SNOW_URL + 'api/now/table/' + SCALR_TABLE + '/{}'.format(server['sys_id']), json=body)
    return r.json()['result']

def validate_request(request):
    if 'X-Signature' not in request.headers or 'Date' not in request.headers:
        logging.debug('Missing signature headers')
        return False
    date = request.headers['Date']
    body = request.data
    expected_signature = binascii.hexlify(hmac.new(SCALR_SIGNING_KEY, body + date, sha1).digest())
    if expected_signature != request.headers['X-Signature']:
        logging.debug('Signature does not match')
        return False
    date = dateutil.parser.parse(date)
    now = datetime.now(pytz.utc)
    delta = abs((now - date).total_seconds())
    return delta < 300


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0')
