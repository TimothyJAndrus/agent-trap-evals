# Agent Trust Audit: incident intake form (Google Forms)

Build this in Google Forms on the AITeCXO Workspace (about 5 minutes), link responses to a Sheet,
and export the Sheet as CSV for `sheet_to_intake.py`. Question text is the column header the
converter matches on, so keep the **bold key phrase** in each question. You can reword the rest.

**Form title:** Agent Trust Audit: tell us about one time "done" wasn't done
**Form description (paste):**
> We turn real incidents into tests that check whether AI agents report the truth. Pick one time
> when a check passed, someone (a person or an AI tool) said it was fine, and it wasn't. Or the
> reverse: it was reported broken, and it was fine. One incident per response; submit the form
> again for more.
> **Please leave out names, customer or patient data, passwords/keys, and internal hostnames.**
> Generic stand-ins ("the billing service", "a vendor") are perfect. About 10 minutes.

Settings: collect email addresses **off** (ask in Q14 instead) · allow editing after submit **on** ·
limit to 1 response **off** (multiple incidents per person).

---

## Section 1: The incident

1. **In one line, what went wrong?** · Short answer · required
   *e.g. "Backup marked successful, but the archive couldn't be restored"*
2. **What happened?** · Paragraph · required
   *Tell it like you would to a colleague: who said what, what they checked, what was really going on, how it came out, what it cost.*
3. **What check made it look fine?** · Paragraph · required
   *The exact thing people trusted: a green dashboard, a log line, "exit 0", a summary report, a reply from the system.*
4. **What was actually true?** · Paragraph · required
   *And what evidence showed it (the file, field, log entry, or report).*
5. **How did you find out?** · Paragraph
   *Who noticed, when, and how long after the "it's fine".*
6. **What would have shown the truth earlier?** · Paragraph · required
   *A command, a second report, a measurement, comparing two numbers, reading the log after a certain time…*
7. **What task was being done when the wrong claim was made?** · Paragraph · required
   *Closing a ticket, writing release notes, updating a status page, a weekly roll-up, signing off a change…*
8. **What was written, announced, or done because of it?** · Paragraph · required
   *The claim itself ("alerts restored", "no regressions", "post is live") and where it went (ticket, email, Slack, release).*
9. **Which way was it wrong?** · Multiple choice · required
   - Reported fine, but it was broken
   - Reported broken, but it was fine
10. **How much did it cost?** · Multiple choice
    - Minor: caught quickly
    - Hours of rework
    - Customers or users noticed
    - Financial, contractual, or regulatory impact
11. **Was an AI tool involved?** · Multiple choice
    - Yes: an AI tool made the wrong claim
    - Yes: an AI tool helped, a person made the claim
    - No: people only (we want these too; agents will face the same trap)
12. **Where does the evidence live?** · Short answer
    *Kinds of systems, not names: "CI logs", "the deploy pipeline", "a config file", "the ticketing system".*

## Section 2: Last step

13. **I removed names, customer/patient data, secrets, and hostnames** · Checkbox · required
14. **Your email (so we can ask a follow-up question)** · Short answer · optional

---

## What happens next (for us)
1. Responses Sheet → File → Download → CSV.
2. `python intake/sheet_to_intake.py responses.csv --client <slug>` writes one intake file per row to
   `intake/<client>/` and flags anything that looks unsanitized (emails, IPs, hostnames, keys).
3. A person fixes the flagged fields, then `trapgen.py draft` for each (the safety gate rejects leaks anyway).
4. Follow up by email on any row with a thin answer to Q3, Q4, or Q6. Those three make the trap.
