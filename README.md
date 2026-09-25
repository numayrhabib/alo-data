# Alo schedule data

This repository feeds the Alo app. Every 30 minutes GitHub runs `fetch_schedules.py`, which checks the DESCO, DPDC, BREB, BPDB, NESCO and WZPDCL websites, reads their load shedding schedules (tables and PDF notices, in Bangla or English), and saves them to `data/schedule.json`.

The app reads: https://raw.githubusercontent.com/numayrhabib/alo-data/main/data/schedule.json

- If a site is down or changes its layout, that site's last good schedule is kept and the problem is listed under `sources` in the file.
- Notices that are only photos are listed under `notices` with a link.
- To change which pages are checked, edit `sources.json`.
- To run a check now: Actions tab, "Fetch load shedding schedules", Run workflow.
