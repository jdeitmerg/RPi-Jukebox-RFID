import threading
import time
import logging
import json
import zmq
import jukebox.publishing.subscriber as subscriber
import jukebox.cfghandler
import jukebox.plugs as plugin

logger = logging.getLogger('jb.led_strip.manager')
cfg = jukebox.cfghandler.get_handler('jukebox')

# State priorities (used as state ID )
PRIO_IDLE = 10
PRIO_CHARGING = 20
PRIO_SYNC = 30
PRIO_BATT_WARNING = 40
PRIO_OVERLAY = 50  # Temporary overlays like volume/battery
PRIO_READY = 60  # Short animation on startup
PRIO_SHUTDOWN = 100


class LedStripManager(threading.Thread):
    def __init__(self, num_leds=16, pin=12, brightness=50, base_color=(255, 255, 255)):
        super().__init__(name='LedStripManager')
        self.port = 5559
        self.daemon = True
        self._keep_running = True
        self.num_leds = num_leds
        self.pin = pin
        self.brightness = brightness
        self.base_color = {'r': base_color[0], 'g': base_color[1], 'b': base_color[2]}

        # RPC Setup
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REQ)
        self.socket.connect(f"tcp://127.0.0.1:{self.port}")
        self.socket.setsockopt(zmq.LINGER, 500)
        self.socket.setsockopt(zmq.RCVTIMEO, 1000)

        # State tracking
        self.current_state = PRIO_IDLE
        self.state_data = {}
        self.overlay_timeout = 0
        self.overlay_start_time = 0
        self.last_sent_state = None

        # Subscriptions
        self.sub = subscriber.Subscriber("inproc://PublisherToProxy", [
            'volume.level', 'sync.status'
        ])

    def _rpc_call(self, method, params=None):
        try:
            self.socket.send_string(json.dumps({'method': method, 'params': params or {}}))
            self.socket.recv_string()  # Wait for ack
        except Exception as e:
            logger.error(f"Failed to call LED daemon: {e}")
            # Reconnect on error
            self.socket.close()
            self.socket = self.context.socket(zmq.REQ)
            self.socket.connect(f"tcp://127.0.0.1:{self.port}")

    def run(self):
        logger.info("LedStripManager started")

        # init has to be the first call
        params = {'num_leds': self.num_leds,
                  'pin': self.pin,
                  'brightness': self.brightness,
                  'base_color': self.base_color}
        self._rpc_call('init', params)

        # Trigger Ready animation
        self.current_state = PRIO_READY
        self.overlay_start_time = time.time()
        self._rpc_call('start_animation', {'name': 'ready'})

        while self._keep_running:
            try:
                topic, payload = self.sub.receive(zmq.NOBLOCK)
                if topic:
                    self._handle_event(topic, payload)
            except zmq.ZMQError:
                # No message received. Delay here so we don't delay when there are messages to process
                time.sleep(0.1)

            # Check timeouts for Ready and Overlays
            now = time.time()
            if self.current_state == PRIO_OVERLAY:
                if now - self.overlay_start_time > self.overlay_timeout:
                    self._reset_state()
            elif self.current_state == PRIO_READY:
                if now - self.overlay_start_time > 3.0:
                    self._reset_state()

            self._update_daemon()

    def _handle_event(self, topic, payload):
        logger.debug(f"Received event on topic '{topic}': {payload}")
        if topic == 'volume.level':
            max_volume = plugin.call('volume', 'ctrl', 'get_soft_max_volume')
            self._trigger_overlay('volume', payload['volume'] / max_volume, duration=5)
        elif topic == 'sync.status':
            self._handle_sync(payload)

    def _trigger_overlay(self, type, value, duration):
        if self.current_state <= PRIO_OVERLAY:
            self.current_state = PRIO_OVERLAY
            self.state_data = {'type': type, 'value': value}
            self.overlay_timeout = duration
            self.overlay_start_time = time.time()

    def _handle_battery(self, payload):
        soc = payload.get('soc', 0) / 100
        warning = soc < .2
        charging = payload.get('charging', 0)

        if warning:
            if self.current_state < PRIO_BATT_WARNING:
                self.current_state = PRIO_BATT_WARNING
                self.state_data = {'soc': soc}
        elif charging:
            if self.current_state < PRIO_CHARGING:
                self.current_state = PRIO_CHARGING
                self.state_data = {'soc': soc}
        elif self.current_state in [PRIO_BATT_WARNING, PRIO_CHARGING]:
            self._reset_state()

    def _handle_sync(self, payload):
        active = payload.get('active', False)
        if active:
            if self.current_state < PRIO_SYNC:
                self.current_state = PRIO_SYNC
        elif self.current_state == PRIO_SYNC:
            self._reset_state()

    def _reset_state(self):
        self.current_state = PRIO_IDLE
        self.state_data = {}

    def _update_daemon(self):
        # Only send command if state changed to keep traffic low
        state_key = (self.current_state, json.dumps(self.state_data, sort_keys=True))
        if state_key == self.last_sent_state:
            return

        self.last_sent_state = state_key

        if self.current_state == PRIO_SHUTDOWN:
            self._rpc_call('start_animation', {'name': 'shutdown'})
        elif self.current_state == PRIO_READY:
            self._rpc_call('start_animation', {'name': 'ready'})
        elif self.current_state == PRIO_OVERLAY:
            if self.state_data['type'] == 'volume':
                self._rpc_call('set_bar', {'ratio': self.state_data['value']})
            elif self.state_data['type'] == 'battery':
                # Battery level is static bar in daemon
                self._rpc_call('set_bar', {'ratio': self.state_data['value']})
        elif self.current_state == PRIO_BATT_WARNING:
            self._rpc_call('set_bar', {'ratio': self.state_data['soc'], 'r': 255, 'g': 0, 'b': 0})
        elif self.current_state == PRIO_SYNC:
            self._rpc_call('start_animation', {'name': 'sync'})
        elif self.current_state == PRIO_CHARGING:
            self._rpc_call('start_animation', {'name': 'charging', 'data': {'soc': self.state_data.get('soc', 0)}})
        else:
            self._rpc_call('set_solid', self.base_color)

    def trigger_shutdown(self):
        self.current_state = PRIO_SHUTDOWN
        self._update_daemon()

    def stop(self):
        self._keep_running = False
        self.socket.close()
        self.context.term()
