# Recipes

A recipe is a hand-written rule: a precise yes/no question, a mode, hold time and cooldown. Using one skips the rule-compiling model call, so it works offline and behaves the same for everyone. Ask Hermes for one by name ("use the package-at-door recipe on stream:door"). You can still set `source`, `roi`, `cooldown_seconds`, `schedule` and `action_prompt` on top. Confidence threshold and k-of-n come from your settings.

General tuning for any recipe: add a `roi` around the interesting area; send "false alarm" on wrong alerts; add `no` examples with `sentinel_teach`; check with `/sentinel test <rule_id>`.

## package-at-door

- **Watches:** a camera facing the door or porch. Mode `trigger`, cooldown 30 min.
- **Question:** is there a package, parcel, envelope or delivery box on the ground or doorstep near the door?
- **Example:** "Use the package-at-door recipe on stream:porch." Alert: "A brown cardboard box is on the doormat."
- **Tips:** set an ROI on the doormat to ignore the street. A package already there when you start counts as true, so the first look may alert; remove it, or ignore that alert. The rule latches until the package is gone, so a second parcel while the first is still there will not alert.

## pet-on-couch

- **Watches:** a living-room camera. Mode `trigger`, cooldown 15 min.
- **Question:** is a dog or cat on the couch or sofa, not on the floor next to it?
- **Example:** "Tell me when the dog gets on the couch" with this recipe; add `action_prompt` such as "remind me to call him off".
- **Tips:** ROI on the couch cushions. Add a `no` example of the pet beside the couch. Raise cooldown if the pet hops on and off.

## print-failure

- **Watches:** a camera pointed at a 3D-printer bed. Mode `condition`, must hold 60 s, cooldown 30 min, confidence at least 0.75.
- **Question:** does the print look failed: loose spaghetti-like filament, a part knocked off the bed, or a large blob on the nozzle?
- **Example:** "Watch the printer on camera:1 with the print-failure recipe." Alert: "Strands of filament are tangled above the part."
- **Tips:** good lighting matters most. Condition rules re-check every `idle_check_seconds` (300 s by default), so a failure is found within about five minutes even without motion; lower that setting for faster detection at higher cost. Add `no` examples of a healthy print at several heights.

## training-loss-plateau

- **Watches:** your screen, with a loss chart visible. Mode `condition`, must hold 15 min, cooldown 60 min. Suggested source `screen`.
- **Question:** does the latest part of the loss curve look flat, with no clear downward trend?
- **Example:** "Use training-loss-plateau on screen:2@0,0,1280,720." Alert: "The curve has flattened around 0.41 for the last stretch."
- **Tips:** use a screen region so only the chart is judged, and keep the chart visible (the rule cannot see a minimised window). The model reads the chart from a still image, so a clear, zoomed chart with a legible axis works much better than a thumbnail. Hold time is long on purpose: a short plateau is normal.

## desk-intruder

- **Watches:** a camera covering your desk. Mode `trigger`, cooldown 10 min.
- **Question:** is a person sitting at, standing at, or reaching onto the desk?
- **Example:** "Alert me if someone is at my desk" with this recipe, plus a `schedule` of work hours away from the desk.
- **Tips:** this fires for you too. Use `schedule` to limit it to hours when you are away, or `/sentinel pause` it when at the desk. An ROI over the chair and desk surface reduces hallway false alarms.

## stove-unattended

- **Watches:** a kitchen camera that sees the stove. Mode `condition`, must hold 5 min, cooldown 30 min.
- **Question:** is a burner on (visible flame, glowing element or lit indicator) with no person visible in the kitchen?
- **Example:** "Use stove-unattended on stream:kitchen." Alert: "A burner is glowing red and the kitchen is empty."
- **Tips:** this is a convenience, not a safety device. A camera cannot see every burner state, and a failed alert, camera, or network means no warning. Keep a real smoke alarm and an automatic shut-off. Frame the stove and the room entrance, and use an ROI that includes both.
