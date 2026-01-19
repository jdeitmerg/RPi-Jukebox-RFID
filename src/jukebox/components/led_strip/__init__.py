import logging
import jukebox.plugs as plugin
import jukebox.cfghandler
from components.led_strip.led_strip_manager import LedStripManager
import time
from datetime import time as dtime

logger = logging.getLogger('jb.led_strip')
cfg = jukebox.cfghandler.get_handler('jukebox')

led_strip_manager = None


@plugin.initialize
def initialize():
    global led_strip_manager
    logger.info("Initializing LED Strip Plugin (Proxy Mode)")

    enabled = cfg.setndefault('led_strip', 'enable', value=False)
    if not enabled:
        logger.info("LED Strip Plugin is disabled")
        return

    num_leds = cfg.setndefault('led_strip', 'num_leds', value=16)
    pin = cfg.setndefault('led_strip', 'pin', value=12)
    brightness = cfg.setndefault('led_strip', 'brightness', value=50)
    base_color = cfg.setndefault('led_strip', 'base_color', value=[255, 255, 255])
    reverse_direction = cfg.setndefault('led_strip', 'reverse_direction', value=False)
    nightmode_enable = cfg.setndefault('led_strip', 'night_mode', 'enable', value=False)
    nightmode_start = cfg.setndefault('led_strip', 'night_mode', 'start', value="18:00:00")
    nightmode_end = cfg.setndefault('led_strip', 'night_mode', 'end', value="07:00:00")
    nightmode_brightness = cfg.setndefault('led_strip', 'night_mode', 'brightness', value=5)

    logger.debug(f"LED Strip Config - num_leds: {num_leds}, pin: {pin}, "
                 f"brightness: {brightness}, base_color: {base_color}, reverse_direction: {reverse_direction}, "
                 f"nightmode_enable: {nightmode_enable}, nightmode_start: {nightmode_start}, "
                 f"nightmode_end: {nightmode_end}, nightmode_brightness: {nightmode_brightness}")

    if nightmode_enable:
        nightmode_times = (dtime.fromisoformat(nightmode_start), dtime.fromisoformat(nightmode_end))
    else:
        nightmode_times = None

    led_strip_manager = LedStripManager(num_leds, pin, brightness, tuple(base_color), reverse_direction, nightmode_times,
                                        nightmode_brightness)
    led_strip_manager.start()


@plugin.atexit
def atexit(signal_id: int, **ignored_kwargs):
    global led_strip_manager
    if led_strip_manager:
        logger.info("Stopping LED Strip Plugin")
        # Trigger shutdown animation if possible
        led_strip_manager.trigger_shutdown()
        time.sleep(2.0)  # Wait for animation
        led_strip_manager.stop()
        led_strip_manager.join()


@plugin.register
def show_battery():
    """Manually trigger battery level display."""
    global led_strip_manager
    if led_strip_manager:
        status = plugin.call_ignore_errors('battmon', 'batt_mon', 'get_batt_status')
        if status:
            led_strip_manager._trigger_overlay('battery', status.get('soc', 0) / 100, duration=5)


@plugin.register
def toggle_night_mode():
    """Toggle night mode manually."""
    # TODO: Implement night mode logic
    logger.info("Toggle Night Mode (not implemented)")
