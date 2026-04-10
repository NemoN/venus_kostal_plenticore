import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys

# Add bundled libraries to path (survives Venus OS updates)
_lib_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib')
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

import requests
from Cryptodome.Cipher import AES
from loggingConfig import logger


REQUEST_TIMEOUT = 10


# Based on https://stackoverflow.com/questions/59053539/api-call-portation-from-java-to-python-kostal-plenticore-inverter
# generates Session token that can be used to authenticate all futher request to the inverters api

def get_session_key(passwd, base_url):
    USER_TYPE = "user"
    AUTH_START = "/auth/start"
    AUTH_FINISH = "/auth/finish"
    AUTH_CREATE_SESSION = "/auth/create_session"
    ME = "/auth/me"

    http_session = requests.Session()
    http_session.headers.update({'Content-type': 'application/json', 'Accept': 'application/json'})

    def randomString(stringLength):
        letters = string.ascii_letters
        return ''.join(random.choice(letters) for i in range(stringLength))

    u = randomString(12)
    u = base64.b64encode(u.encode('utf-8')).decode('utf-8')

    step1 = {
        "username": USER_TYPE,
        "nonce": u
    }
    step1 = json.dumps(step1)

    url = base_url + AUTH_START
    response = http_session.post(url, data=step1, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response = response.json()
    i = response['nonce']
    e = response['transactionId']
    o = response['rounds']
    a = response['salt']
    bitSalt = base64.b64decode(a)

    def getPBKDF2Hash(password, bytedSalt, rounds):
        return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytedSalt, rounds)

    r = getPBKDF2Hash(passwd, bitSalt, o)
    s = hmac.new(r, "Client Key".encode('utf-8'), hashlib.sha256).digest()
    c = hmac.new(r, "Server Key".encode('utf-8'), hashlib.sha256).digest()
    _ = hashlib.sha256(s).digest()
    d = "n=user,r=" + u + ",r=" + i + ",s=" + a + ",i=" + str(o) + ",c=biws,r=" + i
    g = hmac.new(_, d.encode('utf-8'), hashlib.sha256).digest()
    p = hmac.new(c, d.encode('utf-8'), hashlib.sha256).digest()
    f = bytes(a ^ b for (a, b) in zip(s, g))
    proof = base64.b64encode(f).decode('utf-8')

    step2 = {
        "transactionId": e,
        "proof": proof
    }
    step2 = json.dumps(step2)

    url = base_url + AUTH_FINISH
    response = http_session.post(url, data=step2, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response = response.json()
    token = response['token']
    signature = response['signature']

    y = hmac.new(_, "Session Key".encode('utf-8'), hashlib.sha256)
    y.update(d.encode('utf-8'))
    y.update(s)
    P = y.digest()
    protocol_key = P
    t = os.urandom(16)

    e2 = AES.new(protocol_key, AES.MODE_GCM, t)
    e2, authtag = e2.encrypt_and_digest(token.encode('utf-8'))

    step3 = {
        "transactionId": e,
        "iv": base64.b64encode(t).decode('utf-8'),
        "tag": base64.b64encode(authtag).decode("utf-8"),
        "payload": base64.b64encode(e2).decode('utf-8')
    }
    step3 = json.dumps(step3)

    url = base_url + AUTH_CREATE_SESSION
    response = http_session.post(url, data=step3, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response = response.json()
    sessionId = response['sessionId']

    # create a new header with the new Session-ID for all further requests
    http_session.headers['authorization'] = "Session " + sessionId
    url = base_url + ME
    response = http_session.get(url=url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response = response.json()
    authOK = response['authenticated']
    if not authOK:
        raise ValueError("Session authentication failed: server returned authenticated=False")

    url = base_url + "/info/version"
    response = http_session.get(url=url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response = response.json()
    swversion = response['sw_version']
    apiversion = response['api_version']
    hostname = response['hostname']
    name = response['name']

    # Fetch inverter settings (serial, product name, max power, string count)
    inv_settings = {'serial': hostname, 'product_name': name, 'max_power': None, 'string_cnt': 3}
    try:
        url = base_url + "/settings/devices:local/Properties:SerialNo,Branding:ProductName1,Inverter:MaxApparentPower,Properties:StringCnt"
        response = http_session.get(url=url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        settings = response.json()
        settings_map = {s['id']: s['value'] for s in settings}
        if 'Properties:SerialNo' in settings_map:
            inv_settings['serial'] = settings_map['Properties:SerialNo']
        if 'Inverter:MaxApparentPower' in settings_map:
            try:
                inv_settings['max_power'] = int(float(settings_map['Inverter:MaxApparentPower']))
            except (ValueError, TypeError):
                pass
        if 'Properties:StringCnt' in settings_map:
            try:
                inv_settings['string_cnt'] = int(float(settings_map['Properties:StringCnt']))
            except (ValueError, TypeError):
                pass
        if 'Branding:ProductName1' in settings_map:
            product_name = settings_map['Branding:ProductName1']
            if inv_settings['max_power'] is not None:
                product_name = product_name + ' ' + str(inv_settings['max_power'])
            inv_settings['product_name'] = product_name
    except Exception as err:
        logger.warning("Could not fetch inverter settings: " + str(err))

    logger.info("Connected to the inverter " + name + "/" + hostname + " (S/N: " + inv_settings['serial'] + ") with SW-Version " + swversion + " and API-Version " + apiversion)
    return sessionId, swversion, apiversion, inv_settings

