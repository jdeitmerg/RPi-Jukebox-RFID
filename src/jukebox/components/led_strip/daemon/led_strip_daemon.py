#!/usr/bin/env python3
import time
import zmq
import json
import logging
import threading
import argparse
import os
import grp
from rpi_ws281x import Color, PixelStrip

SOCKET_PATH = '/tmp/led_strip_daemon.sock'

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('led_strip_daemon')


class LedManager:
    def __init__(self, num_leds, pin, base_color, brightness=50):
        self.cur_animation = None
        self.anim_start_time = 0
        self.anim_data = {}
        self.lock = threading.Lock()
        self.num_leds = num_leds
        self.strip = PixelStrip(num_leds, pin, brightness=brightness)
        self.strip.begin()
        self.base_color = Color(base_color['r'], base_color['g'], base_color['b'])

        logger.info(f"LED Daemon initialized on pin {pin} with {num_leds} LEDs")

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

    def set_battery_bar(self, ratio):
        with self.lock:
            self.cur_animation = None
            self._render_battery_bar(ratio)
            self.strip.show()
            logger.debug(f"Set battery bar: {ratio * 100}%")

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

        # Dim background. Again don't use strip.setBrightness to avoid resetting later
        r_bg = int(self.base_color.r * 0.1)
        g_bg = int(self.base_color.g * 0.1)
        b_bg = int(self.base_color.b * 0.1)
        for i in range(self.num_leds):
            if i < current_num and i < target_num:
                self.strip.setPixelColor(i, self._battery_color(i))
            else:
                self.strip.setPixelColor(i, Color(r_bg, g_bg, b_bg))

    def _battery_color(self, index_from_right, scale=1.0):
        ratio = (index_from_right + 1) / self.num_leds
        if ratio <= 0.2:
            r, g, b = 255, 0, 0
        elif ratio <= 0.4:
            r, g, b = 255, 255, 0
        else:
            r, g, b = 0, 255, 0
        return Color(int(r * scale), int(g * scale), int(b * scale))

    def _render_battery_bar(self, ratio):
        ratio = max(0.0, min(1.0, ratio))
        num_lit = int(ratio * self.num_leds)
        partial_brightness = ratio * self.num_leds - num_lit

        for i in range(self.num_leds):
            if i < num_lit:
                self.strip.setPixelColor(i, self._battery_color(i))
            elif i == num_lit and partial_brightness > 0:
                self.strip.setPixelColor(i, self._battery_color(i, partial_brightness))
            else:
                self.strip.setPixelColor(i, Color(0, 0, 0))

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
    def __init__(self, led_mgr):
        self.led_mgr = led_mgr

    def __enter__(self):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(f'ipc://{SOCKET_PATH}')
        self.socket.setsockopt(zmq.RCVTIMEO, 20)  # 20ms timeout for non-blocking receive
        # Make sure users other than root can access the newly created socket
        group = grp.getgrnam('users').gr_gid
        os.chown(SOCKET_PATH, 0, group)  # 0 is root uid
        os.chmod(SOCKET_PATH, 0o660)     # Ensure it's readable/writable by the group
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.socket.close()
        self.context.term()

    def _process_request(self, request):
        logger.debug(f"Received request: {request}")
        method = request.get('method')
        params = request.get('params', {})

        match method:
            case 'init':
                num_leds = params.get('num_leds', 16)
                pin = params.get('pin', 12)
                brightness = params.get('brightness', 50)
                base_color = params.get('base_color', {'r': 255, 'g': 255, 'b': 255})
                self.led_mgr.init(num_leds, pin, base_color, brightness)
            case 'set_solid':
                self.led_mgr.set_solid(params.get('r', 0), params.get('g', 0), params.get('b', 0))
            case'set_bar':
                self.led_mgr.set_bar(params.get('ratio', 0),
                                    params.get('r', 255),
                                    params.get('g', 255),
                                    params.get('b', 255))
            case 'set_battery':
                self.led_mgr.set_battery_bar(params.get('ratio', 0))
            case'start_animation':
                self.led_mgr.start_animation(params.get('name'), params.get('data'))
            case'stop_animation':
                self.led_mgr.cur_animation = None
            case 'ping':
                pass  # Just respond with 'ok'
            case 'exit':
                logger.info("Shutting down LED Daemon as per request")
                os._exit(0)
            case _:
                logger.warning(f"Unknown method: {method}")

        self.socket.send_string(json.dumps({'status': 'ok'}))

    def receive_and_process(self):
        try:
            message = self.socket.recv_string()
            request = json.loads(message)
            self._process_request(request)
        except zmq.Again:
            pass  # No request received
        self.led_mgr.update_animation()


def main():
    parser = argparse.ArgumentParser(description="LED Strip Daemon")
    parser.add_argument("--pin", type=int, default=12, help="GPIO pin to which the LED strip is connected")
    parser.add_argument("--num-leds", type=int, default=16, help="Number of LEDs in the strip")
    parser.add_argument("--brightness", type=int, default=50, help="Brightness of the LED strip (0-100)")
    parser.add_argument("--base-color", type=str, default="255,255,255", help="Base color in R,G,B format")
    args = parser.parse_args()

    led_mgr = LedManager(
        num_leds=args.num_leds,
        pin=args.pin,
        brightness=args.brightness,
        base_color=dict(zip(['r', 'g', 'b'], map(int, args.base_color.split(','))))
    )

    with MsgHandler(led_mgr) as handler:
        logger.info("LED Daemon listening on IPC socket")
        while True:
            handler.receive_and_process()
            time.sleep(0.01)  # Sleep even if we just processed a request
    # Don't clear on exit to keep last state visible


if __name__ == "__main__":
    main()
