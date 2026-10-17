# Fixes to apply (noted 17 Oct 2026, after the real-battery testing and demo)

Status: [ ] to do, [x] done (commit in brackets once pushed).

1. [x] Gateway power ceiling: set BMS_MAX_POWER_KW to 50 (not 5). The BMS's own operator caps are 50 kW each way; Invinity said the system could do more (about 80 kW) but it is not allowed to.
2. [x] Remove the 500 kW (and 3600 s) defaults from the Run now form and from the Schedule form. Replace them with helpful placeholder text that tells the user what they can type.
3. [ ] Schedule display of long events: an event of 60 hours starting 22 Oct 09:00 showed "9:00 AM to 9:00 PM" with no dates, and the calendar did not show it spanning the days it covers.
4. [ ] Units: the battery charge level should show 2 decimals, and other values where needed.
5. [ ] "Failures (24 h)" on the Overview showed 10 201: are they true, and is it a bad sign? (investigate, then make the number meaningful)
6. [ ] "Charge level over time": a clear chart, with the data stored for a long time. After cutting a test or losing the connection and refreshing the page, the chart went flat and you could not tell when an event happened.
7. [ ] "Power: asked for vs actually flowing": same, a clear chart.
