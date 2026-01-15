#!/usr/bin/env python3
import time
import zmq
import json
import logging
import threading
from rpi_ws281x import Color, PixelStrip

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('led_daemon')


class LedDaemon:
    def __init__(self):
        self.cur_animation = None
        self.anim_start_time = 0
        self.anim_data = {}
        self._keep_running = True
        self.lock = threading.Lock()
        self.initialized = False

    def init(self, num_leds, pin, base_color, brightness=50):
        self.num_leds = num_leds
        self.strip = PixelStrip(num_leds, pin, brightness=brightness)
        self.strip.begin()
        self.base_color = Color(base_color['r'], base_color['g'], base_color['b'])

        self.initialized = True
        logger.info(f"LED Daemon initialized on pin {pin} with {num_leds} LEDs)")

    def set_solid(self, r, g, b):
        with self.lock:
            self.cur_animation = None
            color = Color(r, g, b)
            for i in range(self.num_leds):
                self.strip.setPixelColor(i, color)
            self.strip.show()
            logger.debug(f"Set solid color: ({r}, {g}, {b})")

    def set_bar(self, ratio, r, g, b):
        with self.lock:
            self.cur_animation = None
            num_lit = int(ratio * self.num_leds)
            # One LED might not be fully lit to create a smoother effect
            partial_brightness = ratio * self.num_leds - num_lit
            color = Color(r, g, b)
            for i in range(num_lit):
                self.strip.setPixelColor(i, color)
            if num_lit < self.num_leds:
                r_c = int(((color >> 16) & 0xFF) * partial_brightness)
                g_c = int(((color >> 8) & 0xFF) * partial_brightness)
                b_c = int((color & 0xFF) * partial_brightness)
                self.strip.setPixelColor(num_lit, Color(r_c, g_c, b_c))
            for i in range(num_lit + 1, self.num_leds):
                self.strip.setPixelColor(i, Color(0, 0, 0))
            self.strip.show()
            logger.debug(f"Set bar: {ratio * 100}%")

    def start_animation(self, name, data=None):
        with self.lock:
            self.cur_animation = name
            self.anim_start_time = time.time()
            self.anim_data = data or {}
            logger.info(f"Started animation: {name}")

    def update_animation(self):
        with self.lock:
            if not self.cur_animation:
                return

            elapsed = time.time() - self.anim_start_time

            if self.cur_animation == 'ready':
                self._pattern_ready(elapsed)
            elif self.cur_animation == 'sync':
                self._pattern_sync(elapsed)
            elif self.cur_animation == 'shutdown':
                self._pattern_shutdown(elapsed)
            elif self.cur_animation == 'charging':
                self._pattern_charging(elapsed, self.anim_data.get('soc', 0))

            self.strip.show()

    def _pattern_ready(self, elapsed):
        # First 1.5s: light up from center outwards. Then quick pulses.
        center = self.num_leds / 2.0
        if elapsed < 1.5:
            # Light up bar from center outwards
            progress = elapsed / 1.5
            for i in range(self.num_leds):
                dist = abs(i + 0.5 - center)
                if dist < progress * (self.num_leds / 2.0):
                    self.strip.setPixelColor(i, self.base_color)
                else:
                    self.strip.setPixelColor(i, Color(0, 0, 0))
        else:
            # Quick pulses
            sub_elapsed = (elapsed - 1.5) % 0.7
            brightness = abs(0.35 - sub_elapsed) / 0.35
            # Scale colors instead of setting brightness directly to avoid having to reset the brightness later
            r = int(self.base_color.r * brightness)
            g = int(self.base_color.g * brightness)
            b = int(self.base_color.b * brightness)
            for i in range(self.num_leds):
                self.strip.setPixelColor(i, Color(r, g, b))

    def _pattern_sync(self, elapsed):
        # Moving dots from edges to center and back
        for i in range(self.num_leds):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        period = 2.0
        progress = (elapsed % period) / period
        pos = abs(0.5 - progress) * 2.0
        idx1 = int(pos * (self.num_leds / 2.0 - 0.5))
        idx2 = self.num_leds - 1 - idx1
        self.strip.setPixelColor(idx1, self.base_color)
        self.strip.setPixelColor(idx2, self.base_color)

    def _pattern_charging(self, elapsed, soc):
        # Moving bar indicating charge level
        period = 3.0
        progress = (elapsed % period) / period
        target_num = int(soc * self.num_leds)
        current_num = int(progress * self.num_leds)

        for i in range(self.num_leds):
            idx = self.num_leds - 1 - i
            if i < current_num and i < target_num:
                if i < self.num_leds * 0.2:
                    color = Color(255, 0, 0)
                elif i < self.num_leds * 0.4:
                    color = Color(255, 255, 0)
                else:
                    color = Color(0, 255, 0)
                self.strip.setPixelColor(idx, color)
            else:
                # Dim background. Again don't use strip.setBrightness to avoid resetting later
                r = int(self.base_color.r * 0.1)
                g = int(self.base_color.g * 0.1)
                b = int(self.base_color.b * 0.1)
                self.strip.setPixelColor(idx, Color(r, g, b))

    def _pattern_shutdown(self, elapsed):
        # Light down from edges to center. Leave center LED (LEDs if there's an even number) on until power is cut
        duration = 2.0
        progress = min(elapsed / duration, 1.0)
        center = self.num_leds / 2.0
        remaining = (1.0 - progress) * (self.num_leds / 2.0)
        partial_brightness = remaining - int(remaining)
        for i in range(self.num_leds):
            dist = abs(i + 0.5 - center)
            if dist <= remaining:
                self.strip.setPixelColor(i, self.base_color)
            elif dist - 1 < remaining and partial_brightness > 0:
                r = int(self.base_color.r * partial_brightness)
                g = int(self.base_color.g * partial_brightness)
                b = int(self.base_color.b * partial_brightness)
                self.strip.setPixelColor(i, Color(r, g, b))
            else:
                self.strip.setPixelColor(i, Color(0, 0, 0))


class MsgHandler():
    def __init__(self, led_daemon, listen_port):
        self.led_daemon = led_daemon
        self.listen_port = listen_port

    def __enter__(self):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(f"tcp://127.0.0.1:{self.listen_port}")
        self.socket.setsockopt(zmq.RCVTIMEO, 20)  # 20ms timeout for non-blocking receive
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.socket.close()
        self.context.term()

    def _process_request(self, request):
        logger.debug(f"Received request: {request}")
        method = request.get('method')
        params = request.get('params', {})

        if method != 'init' and not self.led_daemon.initialized:
            self.socket.send_string(json.dumps({'status': 'error', 'message': 'Daemon not initialized'}))
            logger.warning("Received command before initialization")
            return

        match method:
            case 'init':
                num_leds = params.get('num_leds', 16)
                pin = params.get('pin', 12)
                brightness = params.get('brightness', 50)
                base_color = params.get('base_color', {'r': 255, 'g': 255, 'b': 255})
                self.led_daemon.init(num_leds, pin, base_color, brightness)
            case 'set_solid':
                self.led_daemon.set_solid(params.get('r', 0), params.get('g', 0), params.get('b', 0))
            case'set_bar':
                self.led_daemon.set_bar(params.get('ratio', 0),
                                    params.get('r', 255),
                                    params.get('g', 255),
                                    params.get('b', 255))
            case'start_animation':
                self.led_daemon.start_animation(params.get('name'), params.get('data'))
            case'stop_animation':
                self.led_daemon.cur_animation = None

        self.socket.send_string(json.dumps({'status': 'ok'}))

    def receive_and_process(self):
        try:
            message = self.socket.recv_string()
            request = json.loads(message)
            self._process_request(request)
        except zmq.Again:
            pass  # No request received
        self.led_daemon.update_animation()


def main():
    daemon = LedDaemon()
    port = 5559

    with MsgHandler(daemon, port) as handler:
        logger.info(f"LED Daemon listening on port {port}")
        while True:
            handler.receive_and_process()
            time.sleep(0.01)  # Sleep even if we just processed a request
    # Don't clear on exit to keep last state visible


if __name__ == "__main__":
    main()
