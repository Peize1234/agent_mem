MIDTERM_PAGE_SUMMARY_PROMPT = """
Summarize one evicted user/assistant dialogue turn for mid-term conversational memory.

Return only a JSON object with:
- summary: a concise sentence about what the user discussed or revealed
- keywords: 3 to 8 short topic keywords

Prefer user intent, preferences, constraints, and topics. Do not make the assistant's long answer
the main topic unless it is essential for understanding the user's request.
"""

MIDTERM_SESSION_MERGE_PROMPT = """
Merge an existing mid-term session summary with a new dialogue page summary.

Return only a JSON object with:
- summary: a concise topic-level summary for the whole session
- keywords: 5 to 12 short topic keywords
"""
