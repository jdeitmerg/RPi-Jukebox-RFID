import threading
import time
import logging
import json
import zmq
import jukebox.publishing.subscriber as subscriber
import jukebox.cfghandler

logger = logging.getLogger('jb.led_strip.manager')
cfg = jukebox.cfghandler.get_handler('jukebox')

# State priorities (used as state ID as well)
PRIO_IDLE = 10
PRIO_CHARGING = 20
PRIO_SYNC = 30
PRIO_BATT_WARNING = 40
PRIO_OVERLAY = 50  # Temporary overlays like volume/battery
PRIO_READY = 60  # Short animation on startup
PRIO_SHUTDOWN = 100


class LedStripManager(threading.Thread):
    def __init__(self, port=5559, kid_color=(255, 255, 255)):
        super().__init__(name='LedStripManager')
        self.port = port
        self.kid_color = kid_color
        self.daemon = True
        self._keep_running = True

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
            'volume.level', 'batt_status', 'sync.status'
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

        # Initial sync of base color
        self._rpc_call('set_base_color', {'r': self.kid_color[0], 'g': self.kid_color[1], 'b': self.kid_color[2]})

        # Trigger Ready animation
        self.current_state = PRIO_READY
        self.overlay_start_time = time.time()
        self._rpc_call('start_animation', {'name': 'ready'})

        while self._keep_running:
            try:
                # We don't need 50Hz here anymore since the daemon handles animations
                topic, payload = self.sub.receive(timeout=0.1)
                if topic:
                    self._handle_event(topic, payload)
            except Exception:
                pass

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
        if topic == 'volume.level':
            self._trigger_overlay('volume', payload, duration=3)
        elif topic == 'batt_status':
            self._handle_battery(payload)
        elif topic == 'sync.status':
            self._handle_sync(payload)

    def _trigger_overlay(self, type, value, duration):
        if self.current_state <= PRIO_OVERLAY:
            self.current_state = PRIO_OVERLAY
            self.state_data = {'type': type, 'value': value}
            self.overlay_timeout = duration
            self.overlay_start_time = time.time()

    def _handle_battery(self, payload):
        soc = payload.get('soc', 0)
        warning = soc < 20
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
                self._rpc_call('set_bar', {'percentage': self.state_data['value']})
            elif self.state_data['type'] == 'battery':
                # Battery level is static bar in daemon
                self._rpc_call('set_bar', {'percentage': self.state_data['value']})
        elif self.current_state == PRIO_BATT_WARNING:
            self._rpc_call('set_bar', {'percentage': self.state_data['soc'], 'r': 255, 'g': 0, 'b': 0})
        elif self.current_state == PRIO_SYNC:
            self._rpc_call('start_animation', {'name': 'sync'})
        elif self.current_state == PRIO_CHARGING:
            self._rpc_call('start_animation', {'name': 'charging', 'data': {'soc': self.state_data.get('soc', 0)}})
        else:
            self._rpc_call('set_solid', {'r': self.kid_color[0], 'g': self.kid_color[1], 'b': self.kid_color[2]})

    def trigger_shutdown(self):
        self.current_state = PRIO_SHUTDOWN
        self._update_daemon()

    def stop(self):
        self._keep_running = False
        self._rpc_call('clear')
        self.socket.close()
        self.context.term()
