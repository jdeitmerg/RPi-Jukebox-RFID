# LED Strip Plugin

This plugin provides a unified, child-friendly LED visualization system for Phoniebox devices equipped with an addressable RGB LED strip (e.g. NeoPixel / WS2812).

The LED strip is used to communicate **system state**, **feedback**, and **ownership** in a way that is:
- Intuitive for children
- Readable at a glance
- Consistent across devices
- Technically simple and robust

## What this plugin does

The LED Strip Plugin controls an addressable RGB LED bar mounted on the front of a Phoniebox.  
It visualizes:

- Box ownership (kid-specific color)
- System lifecycle (off, booting, ready)
- Battery status (level, low warning)
- Charging status
- Volume changes
- Sync status (triggered via RFID card)

The plugin exposes a **state-based LED interface** that other parts of Phoniebox can trigger without needing to know LED-level implementation details.


## Design principles

1. **Kid color is the base identity**
   - The box’s assigned color is the default LED color
   - It should be visible whenever possible

2. **Motion indicates activity**
   - Static = stable / ready
   - Pulsing = waiting / ongoing
   - Moving = active process
   - Blinking = warning or error

3. **Overlays are temporary**
   - Volume and battery displays briefly override the base state
   - After a timeout, LEDs return to the previous state

4. **Warnings must be noticeable but not annoying**
   - Low battery warning is constant pattern, no pulsing or flashing

## LED strip assumptions

- Addressable RGB LEDs (e.g. WS2812 / NeoPixel)
- Any length (recommended: 8–16 LEDs)
- Linear layout, left → right
- Brightness globally configurable
- Connected to pin supporting PWM (GPIO 12, 13, 18 or 19)

The plugin logic scales automatically with LED count. It supports the LED strip being wired from left to right or right to left (configurable)

## States and visual specification

### 1. Kid Identity / Idle (Base State)

**When**
- System is ready
- No higher-priority state active

**Effect**
- Entire LED strip: solid kid color
- No animation

### 2. Booting

**When**
- System startup
- Services not yet ready

**Effect**
- Dark background
- Single dot or short segment in kid color
- Moves left → right repeatedly

**Special considerations**
- The LEDs cannot be controlled from the host during boot. This state has to be implemented by a separate piece of electronics and is not part of this plugin.

### 3. Ready / Booted

**When**
- System fully initialized

**Effect**
- One-time “welcome” pulse in kid color: Fill bar from center (1.5s), fade out and in twice (0.7s each)
- Then switch to Idle (solid kid color)

### 4. Battery Level Display

**When**
- Explicit trigger (e.g. button, RFID)

**Effect**
- Fill right to left, coloring 3 sections: red (< 20%), yellow (< 40%), green (rest)
- Number of lit LEDs proportional to battery percentage
- Display duration: 5 seconds

### 5. Battery Low Warning

**When**
- Battery below 20%

**Effect**
- Constantly show battery level as specified above

### 6. Charging

**When**
- External power connected
- Battery not yet full

**Effect**
- Dim kid color background
- Fill animation of battery level specified above, going from 0% to 100%
- Continuous animation

### 7. Charging Complete (Battery Full)

**When**
- Charging finished

**Effect**
- Constant full battery level as specified above

### 8. Volume Change

**When**
- Volume up/down action

**Effect**
- 5s temporary overlay
- Right-to-left fill representing volume level
- Color: white

### 9. Sync Status

**When**
- Sync process ongoing

**Effect**
- Two dots in kid color
- Moving symmetrically in and out from center to edges
- Continuous animation until sync done

### 10. Shutdown

**When**
- Shutdown command issued

**Effect**
- Animation ending in static pattern that stays on until power is cut
- Full bar in kid's color reducing inward to only center LED (2 LEDs if the number of LEDs is even)

## State priority order

From highest to lowest priority:

1. Shutdown
6. Ready animation after booting
5. Temporary overlays (volume, battery when manually triggered)
1. Critical battery warning
4. Sync animations
2. Charging, charging complete
6. Idle / kid color

Higher-priority states override lower ones.

## Implementation notes

- The plugin should use the available plugin infrastructure to gather the information it needs to display
- The plugin should use the subscription API whenever possible in order to react to state changes asynchronously
- For the battery state trigger, a function should be exposed for wiring up in gpio.yaml
- No other component should directly manipulate LEDs
- Animations should be non-blocking
- Static patters should still be updated at least every 10 seconds to fix the displayed pattern in case of corrupted
  communication (there is neither error correction nor feedback)
- Brightness should be globally configurable
- Basic gamma correction should be used to keep perceived colors constants with varying brightness

## Night Mode

Night Mode reduces visual distraction in dark environments (e.g. bedtime listening) while keeping all system feedback functional.

### Scope

Night Mode affects all states.

### Behavior

- The brightness is reduced to a configurable level.
- The brightness is reduced based on the time of the day, with configurable beginnin and end.
- The plugin exposes a function to toggle night mode manually, overwriting the state based on time of the day.

## ToDo

- Fix "Module components.led_strip.led_strip_manager not loaded as plugin" error
- Ignore volume update on boot
- Night mode
- `invert_direction` configuration flag
- Battery bar sections (red, yellow, green)
- Battery alert
- Test all states defined above
