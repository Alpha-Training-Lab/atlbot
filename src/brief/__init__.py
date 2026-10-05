"""The weekly community brief, posted in the induction group every Monday.
Fully autonomous: nobody writes highlights and nobody approves it.

  capture.py  stores text messages from the groups the brief reads
  digest.py   after each UTC day: counts + a Gemini digest, then deletes
              that day's messages
  weekly.py   Monday from 09:00 UTC: numbers from SQLite, plus Gemini's
              highlights if they pass two checks; otherwise numbers only

Never read: the leadership, onboarding and induction groups
(config.BRIEF_NEVER_READ). Members are named only if they said Yes to the
"Weekly brief" KYC question.
"""
