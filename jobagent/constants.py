"""Application statuses, labels and ordering."""

STATUS_LABELS = {
    "review": "Needs review",
    "queued": "Queued",
    "ready": "Prepared",
    "applied": "Applied",
    "acknowledged": "Acknowledged",
    "replied": "Replied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "ghosted": "No reply",
    "withdrawn": "Withdrawn",
    "manual": "Apply manually",
    "failed": "Failed",
    "dismissed": "Dismissed",
}

# Applications that have been sent and are waiting on / in conversation with a company.
SENT_STATUSES = ("applied", "acknowledged", "replied", "interviewing", "ghosted")
ACTIVE_STATUSES = ("applied", "acknowledged", "replied", "interviewing")
CLOSED_STATUSES = ("rejected", "withdrawn", "offer", "dismissed")

# Higher rank wins when an incoming email suggests a new status.
STATUS_RANK = {
    "review": 0, "queued": 0, "manual": 0, "failed": 0, "dismissed": 0,
    "ready": 1, "applied": 2, "ghosted": 2, "acknowledged": 3, "replied": 4,
    "interviewing": 5, "offer": 6, "rejected": 6, "withdrawn": 6,
}

# Email classification -> application status.
EVENT_TO_STATUS = {
    "ack": "acknowledged",
    "reply": "replied",
    "interview_invite": "interviewing",
    "offer": "offer",
    "rejection": "rejected",
}

EVENT_LABELS = {
    "found": "Job found",
    "approved": "Approved",
    "prepared": "Form prepared",
    "applied": "Application sent",
    "manual": "Needs manual step",
    "failed": "Apply failed",
    "ack": "Receipt confirmed",
    "reply": "Reply received",
    "interview_invite": "Interview invitation",
    "interview": "Interview",
    "offer": "Offer",
    "rejection": "Rejection",
    "ghosted": "No reply",
    "status": "Status changed",
    "note": "Note",
}
