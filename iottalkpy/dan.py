'''
This module wraps the mqtt API into IoTtalk client API

If your process contain sigle Device,
you can use::

    import dan
    dan.register(...)


Or your process contain multiple Device,
you can use::

    from dan import Client

    # for device 1
    dan1 = Client()
    dan1.register(...)

    # for device 2
    dan2 = Client()
    dan2.register(...)

'''
import json
import requests
import logging

from threading import Lock
from uuid import UUID, uuid4

from paho.mqtt import client as mqtt

DAN_COLOR = "\033[1;35m"
DEFAULT_COLOR = "\033[0m"
DATA_COLOR = "\033[1;33m"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("{}DAN{}".format(DAN_COLOR, DEFAULT_COLOR))


def _wrapc(color, s):
    """
    wrap string with color
    """
    return "{}{}{}".format(color, s, DEFAULT_COLOR)


class NoData():
    pass


class DeviceFeature(object):
    def __init__(self, df_name, df_type=None):
        self._df_name = df_name
        self._df_type = df_type if df_type is not None else [None]
        self._push_data = None
        self._on_data = None

    @property
    def df_name(self):
        return self._df_name

    @df_name.setter
    def df_name(self, value):
        self._df_name = value

    @property
    def df_type(self):
        return self._df_type

    @df_type.setter
    def df_type(self, value):
        self._df_type = value

    @property
    def on_data(self):
        return self._on_data

    @on_data.setter
    def on_data(self, value):
        if value is None or not callable(value):
            msg = '<{df_name}>: function not found'.format(df_name=self.df_name)
            raise RegistrationError(msg)
        self._on_data = value

    @property
    def push_data(self):
        return self._push_data

    @push_data.setter
    def push_data(self, value):
        if value is None or not callable(value):
            msg = '<{df_name}>: function not found.'.format(df_name=self.df_name)
            raise RegistrationError(msg)
        self._push_data = value

    def profile(self):
        return (self.df_name, self.df_type)


class ChannelPool(dict):
    def __init__(self):
        self.rtable = {}

    def __setitem__(self, df, topic):
        dict.__setitem__(self, df, topic)
        self.rtable[topic] = df

    def __delitem__(self, df):
        del self.rtable[self[df]]
        dict.__delitem__(self, df)

    def df(self, topic):
        return self.rtable.get(topic)


class Context(object):
    def __init__(self):
        self.url = None
        self.app_id = None
        self.name = None
        self.mqtt_host = None
        self.mqtt_port = None
        self.mqtt_client = None
        self.i_chans = ChannelPool()
        self.o_chans = ChannelPool()
        self.rev = None
        self.on_signal = None
        self.on_data = None
        self.register_callback = None

    def __str__(self):
        return '[{}/{}, mqtt://{}:{}]'.format(
            self.url, self.app_id,
            self.mqtt_host, self.mqtt_port
        )


class RegistrationError(Exception):
    pass


class ApplicationNotFoundError(Exception):
    pass


class AttributeNotFoundError(Exception):
    pass


def _invalid_url(url):
    ''' Check if the url is a valid url
    # This method should be refined
    >>> _invalid_url(None)
    True
    >>> _invalid_url('')
    True
    '''
    return url is None or url == ''


class Client(object):
    def __init__(self):
        self.context = Context()

        self._online_msginfo = None

        self._sub_lock = Lock()  # lock for ctrl channel subscribe finished
        self._sub_lock.acquire()

        self._is_reconnect = False

    def _on_connect(self, client, userdata, flags, rc):

        if not self._is_reconnect:
            log.info('Successfully connect to %s.',
                     _wrapc(DATA_COLOR, self.context.url))
            log.info('Device ID: %s.',
                     _wrapc(DATA_COLOR, self.context.app_id))
            log.info('Device name: %s.',
                     _wrapc(DATA_COLOR, self.context.name))
            client.on_subscribe = self._on_ctrl_sub
            client.subscribe(self.context.o_chans['ctrl'], qos=2)

            self._online_msginfo = client.publish(
                self.context.i_chans['ctrl'],
                json.dumps({'state': 'online', 'rev': self.context.rev}),
                retain=True,
                qos=2
            )
        else:  # in case of reconnecting, we need to renew all subscriptions
            log.info('Reconnect: %s.', _wrapc(DATA_COLOR, self.context.name))
            for k, topic in self.context.o_chans.items():
                log.info('Renew subscriptions for %s -> %s',
                         _wrapc(DATA_COLOR, k), _wrapc(DATA_COLOR, topic))
                client.subscribe(topic, qos=2)

        if self.context.register_callback:
            self.context.register_callback()

        self._is_reconnect = True

    def _on_ctrl_sub(self, client, userdata, mid, qos):
        client.on_subscribe = None
        self._sub_lock.release()

    def _on_message(self, client, userdata, msg):
        if self.context.mqtt_client is not client:
            # drop messages that comes after deregistration
            return

        payload = msg.payload.decode('utf8')
        if msg.topic == self.context.o_chans['ctrl']:
            signal = json.loads(payload)
            if signal['command'] == 'CONNECT':
                if 'idf' in signal:
                    idf = signal['idf']
                    self.context.i_chans[idf] = signal['topic']
                    handling_result = self.context.on_signal(
                        signal['command'], [idf]
                    )

                elif 'odf' in signal:
                    odf = signal['odf']
                    self.context.o_chans[odf] = signal['topic']
                    handling_result = self.context.on_signal(
                        signal['command'], [odf]
                    )
                    # TODO: make ``qos`` configurable
                    client.subscribe(self.context.o_chans[odf])

            elif signal['command'] == 'DISCONNECT':
                if 'idf' in signal:
                    idf = signal['idf']
                    del self.context.i_chans[idf]
                    handling_result = self.context.on_signal(
                        signal['command'], [idf]
                    )

                elif 'odf' in signal:
                    odf = signal['odf']
                    client.unsubscribe(self.context.o_chans[odf])
                    del self.context.o_chans[odf]
                    handling_result = self.context.on_signal(
                        signal['command'], [odf]
                    )

            res_message = {
                'msg_id': signal['msg_id'],
            }
            if handling_result is True:     # user may return (False, 'reason')
                res_message['state'] = 'ok'
            else:
                res_message['state'] = 'error'
                res_message['reason'] = handling_result[1]

            self.context.mqtt_client.publish(
                self.context.i_chans['ctrl'],
                json.dumps(res_message),
                qos=2,
            )

        else:
            df = self.context.o_chans.df(msg.topic)
            if not df:
                return
            self.context.on_data(df, json.loads(payload))

    def _on_offline_pub(self, client, userdata, mid):
        client.disconnect()

    def _on_disconnect(self, client, userdata, rc):
        log.info('Disconnect to %s.', _wrapc(DATA_COLOR, self.context.url))
        if hasattr(self, '_disconn_lock'):  # we won't have it if reconnecting
            self._disconn_lock.release()

    def register(self, url, on_signal, on_data,
                 id_=None, name=None,
                 idf_list=None, odf_list=None,
                 accept_protos=None,
                 profile=None, register_callback=None):
        ''' Register to an IoTtalk server.

        :param url: the url of Iottalk server
        :param on_signal: the signal handler
        :param on_data: the data handler
        :param id_: the uuid used to identify an application, if not provided,
                    this function generates one and return
        :param name: the name of the application
        :param idf_list: the Input Device Feature list of the application.
                         Every element should be a tuple,
                         with the feature name and unit information provided,
                         e.g. ('meow', ('dB'))
        :param odf_list: the Output Device Feature list of the application.
        :param accept_protos: the protocols accepted by the application
        :param profile: an abitrary json data field
        :param register_callback: the callable function invoked
               while `on_connect` successful.
        :type url: str
        :type on_signal: Function
        :type on_data: Function
        :type id_: str
        :type name: str
        :type idf_list: List[Tuple[str, List[str]]]
        :type odf_list: List[Tuple[str, List[str]]]
        :type accept_protos: List[str]
        :type profile: dict
        :returns: the json object responsed from server if registration succeed
        :raises: RegistrationError if already registered or registration failed
        '''
        if self.context.mqtt_client:
            raise RegistrationError('Already registered')

        self.context.url = url
        if _invalid_url(self.context.url):
            raise RegistrationError('Invalid url: "{}"'.format(self.context.url))

        try:
            self.context.app_id = UUID(id_) if id_ else uuid4()
        except ValueError:
            raise RegistrationError('Invalid UUID: {!r}'.format(id_))

        body = {}
        if name:
            body['name'] = name

        if idf_list:
            body['idf_list'] = idf_list

        if odf_list:
            body['odf_list'] = odf_list

        body['accept_protos'] = accept_protos

        if profile:
            body['profile'] = profile

        self.context.register_callback = register_callback

        try:
            response = requests.put(
                '{}/{}'.format(self.context.url, self.context.app_id),
                headers={
                    'Content-Type': 'application/json',
                },
                data=json.dumps(body)
            )

            if response.status_code != 200:
                raise RegistrationError(response.json()['reason'])
        except requests.exceptions.ConnectionError:
            raise RegistrationError('ConnectionError')

        metadata = response.json()
        ctx = self.contex
        ctx.name = metadata['name']
        ctx.mqtt_host = metadata['url']['host']
        ctx.mqtt_port = metadata['url']['port']
        ctx.i_chans['ctrl'] = metadata['ctrl_chans'][0]
        ctx.o_chans['ctrl'] = metadata['ctrl_chans'][1]
        ctx.rev = rev = metadata['rev']
        ctx.mqtt_client = mqtt.Client(client_id='iottalk-py-{}'.format(uuid4.hex()))
        ctx.mqtt_client.on_message = self._on_message
        ctx.mqtt_client.on_connect = self._on_connect
        ctx.mqtt_client.on_disconnect = self._on_disconnect

        ctx.mqtt_client.will_set(
            self.context.i_chans['ctrl'],
            json.dumps({'state': 'broken', 'rev': rev}),
            retain=True,
        )
        ctx.mqtt_client.connect(
            self.context.mqtt_host,
            port=self.context.mqtt_port,
        )

        ctx.mqtt_client.loop_start()

        ctx.on_signal = on_signal
        ctx.on_data = on_data

        self._online_msginfo.wait_for_publish()  # wait for online message published
        log.debug('Online info published')
        self._sub_lock.acquire()  # wait for ctrl channel subscribed

        return self.context

    def deregister(self):
        ''' Deregister from an IoTtalk server.

        This function will block until the offline message published and
        DELETE request finished.

        :raises: RegistrationError if not registered or deregistration failed
        '''
        if not self.context.mqtt_client:
            raise RegistrationError('Not registered')

        # FIXME: replace lock with ``wait_for_publish``
        self._disconn_lock = Lock()
        self._disconn_lock.acquire()

        self.context.mqtt_client.on_publish = self._on_offline_pub
        self.context.mqtt_client.publish(
            self.context.i_chans['ctrl'],
            json.dumps({'state': 'offline', 'rev': self.context.rev}),
            retain=True,
            qos=2,
        )

        try:
            response = requests.delete(
                '{}/{}'.format(self.context.url, self.context.app_id),
                headers={
                    'Content-Type': 'application/json'
                },
                data=json.dumps({'rev': self.context.rev})
            )

            if response.status_code != 200:
                raise RegistrationError(response.json()['reason'])
        except requests.exceptions.ConnectionError:
            raise RegistrationError('ConnectionError')

        self._disconn_lock.acquire()  # wait for disconnect finished
        del self._disconn_lock
        self.context.mqtt_client = None

        return response.json()

    def push(self, idf, data, block=False):
        '''
        Push data to IoTtalk server.

        :param block: if ``True``, block mqtt publishing util finished
        :returns: ``True`` if publishing fired, ``False`` if failed
        :raises: RegistrationError if not registered
        '''
        ctx = self.context
        if not ctx.mqtt_client:
            raise RegistrationError('Not registered')

        if ctx.i_chans.get(idf) is None:
            return False

        data = data if isinstance(data, list) else [data]
        data = json.dumps(data)

        # TODO: make qos configurable
        pub = ctx.mqtt_client.publish(
            self.context.i_chans[idf],
            data,
        )

        if block:
            pub.wait_for_publish()

        return True

    def loop_forever(self):
        if not self.context or not self.context.mqtt_client:
            log.error('please register first')
            return

        self.context.mqtt_client.loop_forever()



_default_client = Client()


def register(*args, **kwargs):
    return _default_client.register(*args, **kwargs)


def deregister():
    return _default_client.deregister()


def push(idf, data, **kwargs):
    return _default_client.push(idf, data, **kwargs)


def loop_forever():
    _default_client.loop_forever()
