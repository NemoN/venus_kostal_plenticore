import json
import requests
import time
from loggingConfig import logger


REQUEST_TIMEOUT = 10

# Reuse HTTP connections via session
http_session = requests.Session()
http_session.headers.update({'Content-type': 'application/json', 'Accept': 'application/json'})

lastTime = 0
lastEnergy = 0
calcEnergy = 0


def reset_energy_state():
    """Reset energy calculation state after reconnect to avoid drift."""
    global lastTime, lastEnergy, calcEnergy
    lastTime = 0
    lastEnergy = 0
    calcEnergy = 0
    logger.info('Energy calculation state reset')

def get_data(baseUrl, sessionId):
    global lastTime
    global lastEnergy
    global calcEnergy

    http_session.headers['authorization'] = "Session " + sessionId
    url = baseUrl + "/processdata/devices:local:ac"

    response = http_session.get(url=url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    processdata = response.json()[0]['processdata']

    def getProcessDataValue(id):
        return next(x['value'] for x in processdata if x['id'] == id)

    data = {}

    data['VA'] = round(getProcessDataValue('L1_U'), 1)
    data['PA'] = round(getProcessDataValue('L1_P'), 1)
    data['IA'] = round(getProcessDataValue('L1_I'), 1)
    data['VB'] = round(getProcessDataValue('L2_U'), 1)
    data['PB'] = round(getProcessDataValue('L2_P'), 1)
    data['IB'] = round(getProcessDataValue('L2_I'), 1)
    data['VC'] = round(getProcessDataValue('L3_U'), 1)
    data['PC'] = round(getProcessDataValue('L3_P'), 1)
    data['IC'] = round(getProcessDataValue('L3_I'), 1)
    data['PT'] = data['PA'] + data['PB'] + data['PC']
    data['IN0'] = round(data['IA'] + data['IB'] + data['IC'], 1)
    data['FREQ'] = round(getProcessDataValue('Frequency'), 2)

    # Fetch DC tracker data and inverter state in a single batch request
    try:
        url = baseUrl + "/processdata"
        batch_payload = [
            {"moduleid": "devices:local:pv1", "processdataids": ["U", "I", "P"]},
            {"moduleid": "devices:local:pv2", "processdataids": ["U", "I", "P"]},
            {"moduleid": "devices:local:pv3", "processdataids": ["U", "I", "P"]},
            {"moduleid": "devices:local", "processdataids": ["Dc_P", "Inverter:State"]},
        ]
        response = http_session.post(url=url, json=batch_payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        batch_result = response.json()

        def getBatchValue(moduleid, dataid):
            for module in batch_result:
                if module['moduleid'] == moduleid:
                    for pd in module['processdata']:
                        if pd['id'] == dataid:
                            return pd['value']
            return 0.0

        for i, pv in enumerate(['pv1', 'pv2', 'pv3'], start=1):
            prefix = 'PV{}_'.format(i)
            data[prefix + 'U'] = round(getBatchValue('devices:local:' + pv, 'U'), 1)
            data[prefix + 'I'] = round(getBatchValue('devices:local:' + pv, 'I'), 2)
            data[prefix + 'P'] = round(getBatchValue('devices:local:' + pv, 'P'), 1)

        data['DC_P'] = round(getBatchValue('devices:local', 'Dc_P'), 1)
        data['INV_STATE'] = int(getBatchValue('devices:local', 'Inverter:State'))
    except Exception as err:
        logger.warning('Failed to fetch DC tracker data: ' + str(err))
        for i in range(1, 4):
            prefix = 'PV{}_'.format(i)
            data[prefix + 'U'] = None
            data[prefix + 'I'] = None
            data[prefix + 'P'] = None
        data['DC_P'] = None
        data['INV_STATE'] = None

    url = baseUrl + "/processdata/scb:statistic:EnergyFlow/Statistic:Yield:Total,Statistic:Yield:Day"
    response = http_session.get(url=url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    yield_data = response.json()[0]['processdata']
    energy = next(x['value'] for x in yield_data if x['id'] == 'Statistic:Yield:Total')
    data['YIELD_DAY'] = round(next((x['value'] for x in yield_data if x['id'] == 'Statistic:Yield:Day'), 0.0) / 1000.0, 3)
    currentTime = time.time()

    # Unfortunately the total energy is only updated every 5 min. This is used by the Victron VRM Portal
    # to calculate the energy consumption.
    # If the portal has no regular updates, the energy consumption is higher than normal.
    # To avoid it, we need a actual energy. So we need to calculate the delta by our own.

    # If a new value is retrieved, use this value and reset the delta calculation.
    if energy != lastEnergy:
        logger.info("Calculated Energy: {} Energy: {}".format(calcEnergy, energy))
        lastEnergy = energy
        calcEnergy = energy
        lastTime = currentTime
    else: # Calculate the delta
        # The formula is E = P * t
        deltaTime = currentTime-lastTime # time since last delta calculation
        # Guard against large time gaps (e.g. after reconnect). Max 5 min delta.
        if deltaTime > 300:
            logger.warning('Large time gap ({:.0f}s), capping delta to 300s'.format(deltaTime))
            deltaTime = 300
        delta = data['PT'] * deltaTime / 3600 # P is given in Watt and delta time should be hour, so divide it by 3600 (60min * 60s = 1h)
        calcEnergy += delta # add the new delta to the total energy
        logger.debug("Calculated Energy: {} Energy: {} Last Energy {}.".format(calcEnergy, energy, lastEnergy))

    data['EFAT'] = round(calcEnergy / 1000.0, 3)
    # also store the energy for all three phases
    data['EA'] = round(data['EFAT']/3.0, 3)
    data['EB'] = round(data['EFAT']/3.0, 3)
    data['EC'] = round(data['EFAT']/3.0, 3)

    # assume we always run ;)
    data['STATUS'] = 'running'
    lastTime = currentTime # record the last time for the next iteration

    return data
