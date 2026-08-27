import re
import json
import os
import time
import datetime
from typing import Any, Dict, List, Optional
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
import base64


def strip_html_to_text(html: str) -> str:
    if not html:
        return ""
    
    text = html
    # Remove style/script blocks entirely
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', text, flags=re.IGNORECASE)
    
    # Convert <br>, <p>, <div>, <tr> to newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|div|tr|li|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<(p|div|tr|li|h[1-6])[^>]*>', '', text, flags=re.IGNORECASE)
    
    # Remove all remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Decode common HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = text.replace('&mdash;', '—')
    text = text.replace('&ndash;', '–')
    
    # Collapse excessive whitespace and normalize line breaks
    text = text.replace('\r\n', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Strip corporate & Grant Thornton email disclaimers
    disclaimer_patterns = [
        r'(?i)(?:and,\s*)?to the fullest extent permitted by law[^\n]*(?:\n[^\n]+){0,15}',
        r'(?i)Grant Thornton [^\n]*accepts no (?:responsibility|liability)[^\n]*(?:\n[^\n]+){0,15}',
        r'(?i)If you have received this e-?mail in error[^\n]*(?:\n[^\n]+){0,15}',
        r'(?i)email communications cannot be guaranteed to be secure or error free[^\n]*(?:\n[^\n]+){0,10}',
        r'(?i)(?:This e-?mail|This message)\s+(?:and any (?:files|attachments)[^\n]+)?\s*is\s+(?:strictly\s+)?confidential[^\n]*(?:\n[^\n]+){0,10}',
        r'(?i)View our disclaimer'
    ]
    for dp in disclaimer_patterns:
        text = re.sub(dp, '', text)

    return text.strip()

def parse_email_addresses(header: str) -> list[str]:
    if not header:
        return []
    email_regex = r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}'
    return re.findall(email_regex, header)

def classify_sender(email: str) -> str:
    if not email:
        return 'external'
    return 'internal' if '@bh.gt.com' in email.lower() else 'external'

def parse_forwarded_email(subject: str, body: str, outer_from: str, outer_to: str) -> dict:
    is_forwarded = bool(re.match(r'^(fw|fwd)\s*:', subject.strip(), flags=re.IGNORECASE))
    
    forwarder_email_list = parse_email_addresses(outer_from)
    forwarder_email = forwarder_email_list[0] if forwarder_email_list else ""
    
    result = {
        "isForwarded": is_forwarded,
        "forwarder": outer_from,
        "forwarderEmail": forwarder_email,
        "forwardedTo": outer_to,
        "originalFrom": "",
        "originalFromEmail": "",
        "originalTo": "",
        "originalCc": "",
        "originalSubject": re.sub(r'^(fw|fwd)\s*:\s*', '', subject, flags=re.IGNORECASE).strip(),
        "originalBody": "",
        "fullBody": body
    }
    
    if not is_forwarded or not body:
        return result
        
    separator_patterns = [
        r'[-]{3,}\s*Original Message\s*[-]{3,}',
        r'[-]{3,}\s*Forwarded Message\s*[-]{3,}',
        r'_{3,}',
        r'\n\s*From:\s+[\w\s]+<[\w.@]+>'
    ]
    
    embedded_block_start = -1
    for pattern in separator_patterns:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            embedded_block_start = match.start()
            break
            
    if embedded_block_start == -1:
        match = re.search(r'\n(From:\s.+?@.+?\n)', body, flags=re.IGNORECASE)
        if match:
            embedded_block_start = match.start()
            
    if embedded_block_start == -1:
        result["originalBody"] = body.strip()
        return result
        
    embedded_block = body[embedded_block_start:]
    
    from_match = re.search(r'From:\s*(.+?)(?:\n|$)', embedded_block, flags=re.IGNORECASE)
    to_match = re.search(r'^To:\s*(.+?)(?:\n|$)', embedded_block, flags=re.IGNORECASE | re.MULTILINE)
    cc_match = re.search(r'^Cc:\s*(.+?)(?:\n|$)', embedded_block, flags=re.IGNORECASE | re.MULTILINE)
    subject_match = re.search(r'^Subject:\s*(.+?)(?:\n|$)', embedded_block, flags=re.IGNORECASE | re.MULTILINE)
    
    if from_match and from_match.group(1):
        result["originalFrom"] = from_match.group(1).strip()
        emails = parse_email_addresses(result["originalFrom"])
        result["originalFromEmail"] = emails[0] if emails else ""
        
    if to_match and to_match.group(1):
        result["originalTo"] = to_match.group(1).strip()
    if cc_match and cc_match.group(1):
        result["originalCc"] = cc_match.group(1).strip()
    if subject_match and subject_match.group(1):
        result["originalSubject"] = subject_match.group(1).strip()
        
    header_block_end = embedded_block.find('\n\n')
    if header_block_end != -1:
        result["originalBody"] = embedded_block[header_block_end:].strip()
    else:
        result["originalBody"] = embedded_block.strip()
        
    return result

def extract_text_from_pdf_base64(base64_data: str) -> str:
    try:
        import base64
        import fitz
        pdf_bytes = base64.b64decode(base64_data)
        doc = fitz.open("pdf", pdf_bytes)
        text = ""
        for page in doc:
            text += page.get_text()
            
        with open("pdf_debug.log", "a") as f:
            f.write(f"SUCCESS: Extracted {len(text)} chars from PDF.\n")
            
        return text[:4000] # truncate to avoid blowing up context window
    except Exception as e:
        with open("pdf_debug.log", "a") as f:
            f.write(f"PDF extraction error: {e}\n")
        print(f"PDF extraction error: {e}")
        return ""

def extract_text_from_docx_base64(base64_data: str) -> str:
    try:
        import base64
        import io
        from docx import Document
        
        docx_bytes = base64.b64decode(base64_data)
        doc_file = io.BytesIO(docx_bytes)
        document = Document(doc_file)
        
        text = "\n".join([paragraph.text for paragraph in document.paragraphs])
        
        with open("pdf_debug.log", "a", encoding="utf-8") as f:
            f.write(f"SUCCESS: Extracted {len(text)} chars from DOCX.\n")
            f.write(f"--- DOCX CONTENT PREVIEW ---\n{text[:1000]}\n---------------------------\n")
            
        return text[:4000] # truncate to avoid blowing up context window
    except Exception as e:
        with open("pdf_debug.log", "a", encoding="utf-8") as f:
            f.write(f"DOCX extraction error: {e}\n")
        print(f"DOCX extraction error: {e}")
        return ""

def clean_and_parse_json(text: str) -> dict:
    """
    Robust JSON parser for LLM outputs.
    Handles <think> tags, markdown fences, trailing commas, unescaped control chars,
    and partial JSON responses.
    """
    if not text or not text.strip():
        return {}

    content = text.strip()

    # 1. Strip reasoning blocks (<think>...</think> and unclosed <think>)
    if "<think>" in content.lower():
        if "</think>" in content.lower():
            content = re.sub(r'(?i)<think>[\s\S]*?</think>', '', content).strip()
        else:
            first_brace = content.find('{')
            if first_brace != -1:
                content = content[first_brace:].strip()
            else:
                content = re.sub(r'(?i)^[\s\S]*?<think>', '', content).strip()

    # 2. Strip markdown fenced code blocks
    if "```" in content:
        m_code = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content, flags=re.IGNORECASE)
        if m_code:
            content = m_code.group(1).strip()

    # 3. Direct JSON load attempt
    try:
        res = json.loads(content)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # 4. Extract outermost JSON object {...}
    first_brace = content.find('{')
    last_brace = content.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        snippet = content[first_brace:last_brace+1].strip()
        try:
            res = json.loads(snippet)
            if isinstance(res, dict):
                return res
        except Exception:
            # Try fixing trailing commas before } or ]
            fixed = re.sub(r',\s*([\}\]])', r'\1', snippet)
            try:
                res = json.loads(fixed)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    # 5. Non-greedy block search
    matches = re.findall(r'\{[\s\S]*?\}', content)
    for m in matches:
        try:
            res = json.loads(m)
            if isinstance(res, dict):
                return res
        except Exception:
            try:
                fixed = re.sub(r',\s*([\}\]])', r'\1', m)
                res = json.loads(fixed)
                if isinstance(res, dict):
                    return res
            except Exception:
                continue

    # 6. Python literal eval fallback
    try:
        import ast
        val = ast.literal_eval(content)
        if isinstance(val, dict):
            return val
    except Exception:
        pass

    return {}


_GQ_MASTER_HIERARCHY_CACHE = {"timestamp": 0, "data": []}
_GQ_CACHE_TTL_SECONDS = 300  # 5 minutes TTL

def get_gq_master_hierarchy(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Retrieves the current active General Query hierarchy from the database:
    Request Type -> Subject -> Query / General Issue.
    Uses dynamic DB queries and in-memory TTL caching.
    """
    global _GQ_MASTER_HIERARCHY_CACHE
    now = time.time()
    if not force_refresh and _GQ_MASTER_HIERARCHY_CACHE["data"] and (now - _GQ_MASTER_HIERARCHY_CACHE["timestamp"] < _GQ_CACHE_TTL_SECONDS):
        return _GQ_MASTER_HIERARCHY_CACHE["data"]

    try:
        from db.database import get_db_engine
        from sqlalchemy import text

        engine = get_db_engine()
        with engine.connect() as conn:
            req_types = conn.execute(
                text("SELECT id, name FROM m_gen_request_type WHERE is_active = 1 ORDER BY id ASC")
            ).mappings().all()

            subjects = conn.execute(
                text("SELECT id, name, request_type_id FROM m_subject WHERE is_active = 1 ORDER BY id ASC")
            ).mappings().all()

            issues = conn.execute(
                text("SELECT id, name, subject_id FROM m_general_issue WHERE is_active = 1 ORDER BY id ASC")
            ).mappings().all()

        issues_by_subject = {}
        for issue in issues:
            s_id = issue["subject_id"]
            if s_id not in issues_by_subject:
                issues_by_subject[s_id] = []
            issues_by_subject[s_id].append({
                "id": issue["id"],
                "name": issue["name"]
            })

        subjects_by_req = {}
        for subj in subjects:
            r_id = subj["request_type_id"]
            if r_id not in subjects_by_req:
                subjects_by_req[r_id] = []
            subjects_by_req[r_id].append({
                "id": subj["id"],
                "name": subj["name"],
                "queries": issues_by_subject.get(subj["id"], [])
            })

        hierarchy = []
        for req in req_types:
            hierarchy.append({
                "id": req["id"],
                "name": req["name"],
                "subjects": subjects_by_req.get(req["id"], [])
            })

        _GQ_MASTER_HIERARCHY_CACHE["timestamp"] = now
        _GQ_MASTER_HIERARCHY_CACHE["data"] = hierarchy
        return hierarchy
    except Exception as e:
        print(f"[get_gq_master_hierarchy] Error loading master hierarchy from DB: {e}")
        return _GQ_MASTER_HIERARCHY_CACHE.get("data", [])


def format_gq_hierarchy_for_prompt(hierarchy: List[Dict[str, Any]]) -> str:
    """
    Formats the CRM General Query master hierarchy cleanly into a compact text block for the LLM prompt.
    """
    lines = []
    for req in hierarchy:
        lines.append(f"Request Type: '{req['name']}'")
        for sub in req.get("subjects", []):
            q_names = [f"'{q['name']}'" for q in sub.get("queries", [])]
            if q_names:
                lines.append(f"  - Subject: '{sub['name']}' -> Valid Queries: [{', '.join(q_names)}]")
            else:
                lines.append(f"  - Subject: '{sub['name']}' -> Valid Queries: []")
    return "\n".join(lines)


def validate_gq_hierarchy(
    raw_req_name: Optional[str],
    raw_sub_name: Optional[str],
    raw_query_name: Optional[str],
    hierarchy: Optional[List[Dict[str, Any]]] = None
) -> tuple:
    """
    Validates that:
    1. Request Type exists in CRM master data.
    2. Subject exists under that Request Type.
    3. Query exists under that Subject.

    Returns (matched_req, matched_sub, matched_query) or None for invalid levels.
    Strictly NO first-option or default fallbacks!
    """
    if hierarchy is None:
        hierarchy = get_gq_master_hierarchy()

    if not raw_req_name:
        return None, None, None

    req_clean = str(raw_req_name).strip().lower()

    # 1. Match Request Type
    matched_req = None
    for req in hierarchy:
        if req["name"].strip().lower() == req_clean:
            matched_req = req
            break
        elif req_clean in req["name"].strip().lower() or req["name"].strip().lower() in req_clean:
            matched_req = req

    if not matched_req:
        return None, None, None

    if not raw_sub_name:
        return matched_req, None, None

    sub_clean = str(raw_sub_name).strip().lower()

    # 2. Match Subject ONLY under valid Subjects of matched_req
    valid_subjects = matched_req.get("subjects", [])
    matched_sub = None
    for sub in valid_subjects:
        if sub["name"].strip().lower() == sub_clean:
            matched_sub = sub
            break
        elif sub_clean in sub["name"].strip().lower() or sub["name"].strip().lower() in sub_clean:
            matched_sub = sub

    if not matched_sub:
        # Invalid relationship: Subject does not belong to Request Type!
        return matched_req, None, None

    if not raw_query_name:
        return matched_req, matched_sub, None

    query_clean = str(raw_query_name).strip().lower()

    # 3. Match Query ONLY under valid Queries of matched_sub
    valid_queries = matched_sub.get("queries", [])
    matched_query = None
    for q in valid_queries:
        if q["name"].strip().lower() == query_clean:
            matched_query = q
            break
        elif query_clean in q["name"].strip().lower() or q["name"].strip().lower() in query_clean:
            matched_query = q

    if not matched_query:
        # Invalid relationship: Query does not belong to Subject!
        return matched_req, matched_sub, None

    return matched_req, matched_sub, matched_query


def resolve_general_query_entities(
    parsed: dict,
    req_type_id: Optional[int],
    sub_id: Optional[int],
    issue_id: Optional[int]
) -> dict:
    """
    Resolves conditional CRM entities (customer_id, project_id, proposal_id, saleslead_id)
    only when explicitly applicable based on the selected General Query classification.
    """
    customer_id = parsed.get("db_customer_id")
    customer_name = parsed.get("db_customer_name") or parsed.get("customer_name")

    intent_lower = str(parsed.get("intent") or "").lower()

    # Proposal is applicable for proposal/EL subjects/issues
    is_proposal_applicable = False
    if sub_id in [16, 17, 29] or (issue_id and issue_id in [39, 53, 54, 55, 56, 57, 58, 111, 112, 113, 114, 115]) or "proposal" in intent_lower or "engagement" in intent_lower:
        is_proposal_applicable = True

    # Sales Lead is applicable for sales lead inquiries
    is_saleslead_applicable = False
    if issue_id == 53 or (sub_id == 16 and not is_proposal_applicable) or "lead" in intent_lower:
        is_saleslead_applicable = True

    # Project is applicable for project-related subjects (sub_id in [11, 12, 13, 28, 30]) AND NOT HR/CRM/IT
    is_project_applicable = False
    if sub_id and sub_id not in [14, 16, 17] and req_type_id not in [1, 5, 9] and not is_proposal_applicable:
        is_project_applicable = True

    resolved_project_id = None
    resolved_proposal_id = None
    resolved_saleslead_id = None

    project_status = "NOT_APPLICABLE"
    proposal_status = "NOT_APPLICABLE"
    saleslead_status = "NOT_APPLICABLE"

    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        with engine.connect() as conn:
            # --- Project Resolution ---
            if is_project_applicable:
                project_hint = parsed.get("project_name")
                proj_rows = []
                if project_hint and len(str(project_hint).strip()) > 2:
                    proj_rows = conn.execute(
                        text("SELECT id, name FROM projects WHERE (LOWER(name) LIKE :p OR LOWER(code) LIKE :p OR LOWER(id) = :pid) AND is_active = 1"),
                        {"p": f"%{str(project_hint).lower().strip()}%", "pid": str(project_hint).lower().strip()}
                    ).fetchall()
                elif customer_id:
                    proj_rows = conn.execute(
                        text("SELECT id, name FROM projects WHERE client = :cid AND is_active = 1"),
                        {"cid": customer_id}
                    ).fetchall()

                if len(proj_rows) == 1:
                    resolved_project_id = proj_rows[0][0]
                    project_status = "FOUND"
                elif len(proj_rows) > 1:
                    exact_matches = [r for r in proj_rows if project_hint and str(project_hint).lower().strip() in r[1].lower()]
                    if len(exact_matches) == 1:
                        resolved_project_id = exact_matches[0][0]
                        project_status = "FOUND"
                    else:
                        resolved_project_id = None
                        project_status = "MULTIPLE_MATCHES"
                else:
                    resolved_project_id = None
                    project_status = "NOT_FOUND"

            # --- Proposal Resolution ---
            if is_proposal_applicable:
                prop_rows = []
                prop_ref = parsed.get("proposal_ref") or parsed.get("task_title")
                if prop_ref and ("prop" in str(prop_ref).lower() or "pr-" in str(prop_ref).lower()):
                    prop_rows = conn.execute(
                        text("SELECT id, code FROM proposal WHERE (LOWER(code) LIKE :ref OR LOWER(id) = :ref_id) AND is_active = 1"),
                        {"ref": f"%{str(prop_ref).lower()}%", "ref_id": str(prop_ref).lower()}
                    ).fetchall()
                elif customer_id:
                    prop_rows = conn.execute(
                        text("SELECT id, code FROM proposal WHERE client_id = :cid AND is_active = 1 ORDER BY created_at DESC"),
                        {"cid": customer_id}
                    ).fetchall()

                if len(prop_rows) == 1:
                    resolved_proposal_id = prop_rows[0][0]
                    proposal_status = "FOUND"
                elif len(prop_rows) > 1:
                    resolved_proposal_id = None
                    proposal_status = "MULTIPLE_MATCHES"
                else:
                    resolved_proposal_id = None
                    proposal_status = "NOT_FOUND"

            # --- Sales Lead Resolution ---
            if is_saleslead_applicable:
                lead_rows = []
                lead_ref = parsed.get("lead_ref") or parsed.get("task_title")
                if lead_ref and ("lead" in str(lead_ref).lower() or "sl-" in str(lead_ref).lower()):
                    lead_rows = conn.execute(
                        text("SELECT id, code FROM saleslead WHERE (LOWER(code) LIKE :ref OR LOWER(id) = :ref_id) AND is_active = 1"),
                        {"ref": f"%{str(lead_ref).lower()}%", "ref_id": str(lead_ref).lower()}
                    ).fetchall()
                elif customer_id:
                    lead_rows = conn.execute(
                        text("SELECT id, code FROM saleslead WHERE customer_id = :cid AND is_active = 1 ORDER BY created_at DESC"),
                        {"cid": customer_id}
                    ).fetchall()

                if len(lead_rows) == 1:
                    resolved_saleslead_id = lead_rows[0][0]
                    saleslead_status = "FOUND"
                elif len(lead_rows) > 1:
                    resolved_saleslead_id = None
                    saleslead_status = "MULTIPLE_MATCHES"
                else:
                    resolved_saleslead_id = None
                    saleslead_status = "NOT_FOUND"

    except Exception as e:
        print(f"[resolve_general_query_entities] Error resolving entities: {e}")

    return {
        "customer_id": customer_id if parsed.get("customer_lookup_status") == "FOUND" else None,
        "customer_name": customer_name,
        "project_id": resolved_project_id,
        "project_status": project_status,
        "proposal_id": resolved_proposal_id,
        "proposal_status": proposal_status,
        "saleslead_id": resolved_saleslead_id,
        "saleslead_status": saleslead_status,
    }


def extract_entities_with_llm(
    text: str, 
    sender_type: str, 
    is_forwarded: bool, 
    attachments: list = None, 
    employee_id: int = 0, 
    reference_id: str = None,
    sender_email: str = None,
    to_emails: str = None,
    subject: str = None
) -> dict:
    import time
    start_time = time.time()
    api_key = os.environ.get("EMAIL_API_KEY") or os.environ.get("GROQ_EMAIL_API_KEY") or os.environ.get("CHATBOT_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not api_key:
        return {
            "intent": "General Task",
            "priority": "Medium",
            "project_name": None,
            "customer_name": None,
            "contact_name": None,
            "contact_phone": None,
            "due_date": None,
            "task_description": "Task extracted from email (API key not set).",
            "service_line_hint": None,
            "sender_designation": None,
            "sender_name": None,
            "task_tag": "Project Task"
        }

    sender_context = "The ORIGINAL SENDER is an INTERNAL EMPLOYEE (bh.gt.com domain). This is an internal task or business lead." if sender_type == 'internal' else "The ORIGINAL SENDER is an EXTERNAL CLIENT or CUSTOMER."
    
    forwarded_note = 'NOTE: This is a FORWARDED email. The "=== FULL EMAIL THREAD ===" section contains the REAL content. The task should typically be assigned to whoever was in the "Forwarded to:" field.' if is_forwarded else ""

    gq_hierarchy_text = format_gq_hierarchy_for_prompt(get_gq_master_hierarchy())

    prompt = f"""You are a precise CRM data extraction assistant for Grant Thornton Bahrain.
{sender_context}
{forwarded_note}

CRITICAL RULES:

RULE 0 — DO NOT HALLUCINATE:
- The examples in these rules are purely for format illustration. DO NOT copy them unless they actually appear in the email.
- If a field is not clearly present in the email, return null for that field.

RULE 1 — project_name:
- Search BOTH the email subject line and email body text for project names, system names, or engagement titles (e.g. "full ERP implementation", "Company XYZ_Audit", "ERP Implementation for Global Logistics Corp").
- Strip testing/subject prefixes like "(Testing)", "Action Required:", "FW:", "RE:". If a project or system implementation is named (e.g. "full ERP implementation"), extract it as project_name!
- Short abbreviations like "BPS", "VAT", "MIS", "CBB" are services, NOT project names. Set null.
- For Leave Request or HR emails, set null.

RULE 2 — customer_name:
- You MUST extract the COMPANY or ORGANIZATION name if present.
- CRITICAL: Search BOTH the email subject line and email body text for company/customer names. Strip testing/subject prefixes like "(Testing)", "Action Required:", "FW:", "RE:", "Hi Sahil,". For example, if subject or body mentions "Proposal Sent for Global Logistics Corp", extract "Global Logistics Corp" as customer_name!
- If no company name is found, but a client/prospect person's name is mentioned, extract that person's name as the customer_name (e.g., "Mr. Usama").
- For Leave Request or HR Request emails, set null.
- NEVER extract the email recipient (e.g. "Dear Mr. Arpit"), internal colleagues, or internal firm network names ("Grant Thornton", "GT Bahrain", "GT Oman", "Grant Thornton Oman") as a customer. Internal firm names are NOT client customers!

RULE 3 — contact_name and contact_phone:
- Extract contact_name if the email mentions a third-party client contact or prospect person by name (e.g., "the client contact is Mr. Usama", "contact: John", "reach out to Sarah").
- ALWAYS extract this even if you also used it for customer_name.
- If a phone number or any contact number is mentioned, extract it as contact_phone (e.g. "39579966").
- NEVER extract internal employees, colleagues, or the person the email is addressed to (e.g. "Dear Mr. Arpit") as the contact_name. Otherwise null.

RULE 4 — intent (choose EXACTLY one):
- "Estimation" = cost estimate request, technical estimation, or if subject mentions "Estimation Pending" (CRITICAL: Prioritize this over "Service Lead" even if "Sales Lead" is in the subject).
- "Service Lead" = new client pitched, new business opportunity (do not use if they are explicitly asking for an estimation or proposal).
- "General Task" = internal update, team task, meeting follow-up, OR if email states an issue was ALREADY rectified, resolved, or sent by mistake ("rectified that", "fixed", "sent by mistake", "disregard").
- "Proposal" = existing client wants a proposal.
- "Engagement Letter" = request or signoff for an engagement letter.
- "Invoice" = billing related.
- "Leave Request" = employee leave application (annual, sick, emergency, maternity, etc.). MUST be used whenever subject or body mentions leave, vacation, day off, absence.
- "HR Request" = other internal HR requests (salary inquiry, documents, onboarding, NOC letter, etc.).

RULE 5 — service_line_hint:
- Extract from context or sender signature. "BPS" = "Business Process Services".
- For Leave Request or HR Request, set null.

RULE 6 — task_description:
- Synthesize a clear, 2-to-3 sentence executive summary of the email content and required action items.
- Summarize the client's request, key project scope/requirements, and required follow-up actions in clean, professional prose.
- DO NOT copy-paste the raw email text or form labels verbatim. Write a synthesized, executive-ready summary of what needs to be done!
- CRITICAL INSTRUCTION: If there is an "=== ATTACHMENT: ... ===" block, YOU MUST extract the key details from the attachment text and synthesize them into this summary.
- For leave requests: "Process leave request from [sender name]. Review the requested dates and handle approval via the HR portal."
- CRITICAL INSTRUCTION: If the email states an issue has ALREADY been rectified, resolved, or sent by mistake (e.g. "rectified that", "sent by mistake"), summarize it as an informational resolution update (e.g. "Sender confirms that the proposal issue has been rectified. Informational update only; no further action required."). DO NOT generate a task instructing the user to "investigate and fix".
- NEVER output meta-commentary like "No description available". Always write something actionable and concise.

RULE 7 — sender_name and sender_designation:
- Extract from the ORIGINAL email signature. Return null if not present in the email text.

RULE 8 — MULTIPLE COMPANIES:
- If the email mentions multiple distinct companies or projects, DO NOT combine them into a single string with "and" or commas.
- Instead, MUST return an ARRAY of strings for customer_name containing each distinct company separately. (e.g. ["Company A", "Company B"]).
- If only one, return a single string.

RULE 9 — due_date:
- Return YYYY-MM-DD if a deadline is mentioned. Otherwise null.

RULE 10 — task_tag:
- Assign a short 1-2 word UI tag for this email based on the intent (e.g., "Service Lead", "Support Request", "Internal Admin", "Project Task"). 

RULE 11 — invoice_amount:
- If the intent is "Invoice" and an amount/currency is clearly mentioned in the email, extract it (e.g., "BHD 1500" or "1500"). Otherwise, return null.

RULE 13 — task_title (PURE ACTION SYNTHESIS):
- Read the email body text and synthesize a 2 to 3 word action title describing ONLY the work/action requested.
- STRICT EXCLUSION: NEVER include customer names, project names, company titles, or tokens (<CUSTOMER_TOKEN_x>, <PROJECT_TOKEN_x>) in task_title. The customer and project are handled separately by dedicated CRM fields!
- Examples of PURE ACTION SYNTHESIS:
  - Email asking to check/verify engagement letters -> "Verify Engagement Letters" or "Engagement Letters Verification"
  - Email asking to process an invoice -> "Process Invoice" or "Invoice Payment Review"
  - Email asking to send proposal for audit -> "Audit Proposal Request"
  - Email reporting missing files -> "Verify Proposal Uploads" or "Missing Proposals Check"
  - Email asking for leave -> "Leave Request Approval"
- DO NOT use generic template words like "Proposal Template IT", "Task Request", or "New Task".
- STRICT MAXIMUM OF 2 OR 3 WORDS. NEVER output customer names, project names, or 4+ words.

RULE 14 — confidence_score and confidence_level:
- Estimate an integer confidence_score (0 to 100) based on clarity of the email and extracted fields.
- Set confidence_level as "high" (>= 80), "medium" (50-79), or "low" (< 50).

RULE 15 — GENERAL QUERY HIERARCHICAL CLASSIFICATION:
Classify the email against the following valid CRM Master Hierarchy options:
{gq_hierarchy_text}

CRITICAL — SEMANTIC INTENT DISAMBIGUATION (read before classifying):

STEP 1 — Determine the BUSINESS INTENT of the email:
Ask yourself: "Is this email about a SALES OPPORTUNITY, or is it a TECHNICAL PROBLEM REPORT?"

A. SALES / BUSINESS DEVELOPMENT INTENT signals (→ Marketing & Business Development):
   - A new prospective client, potential customer, or external prospect is introduced
   - Someone is pitching a new engagement, business opportunity, or service requirement
   - Words like: "new client", "prospective", "discovery call", "initial meeting", "business opportunity",
     "request for proposal", "RFP", "quotation", "budget", "implementation requirement", "new project",
     "new engagement", "client onboarding", "new service", "interested in", "seeking services"
   - The email describes WHAT A CLIENT WANTS TO BUY, not a technical failure
   - The mention of any software/technology (e.g. "CRM software", "ERP system", "accounting system")
     in the context of what a CLIENT WANTS TO IMPLEMENT is a SALES OPPORTUNITY, NOT a technical issue
   → Use: Marketing & Business Development → Proposal Request or EL Request or Marketing
   → IMPORTANT: "CRM software implementation for a client" = SALES INTENT, NOT CRM Issues or IT Support

B. TECHNICAL PROBLEM / SUPPORT INTENT signals (→ IT Support or CRM Issues):
   - An EXISTING user or employee is reporting a BROKEN or MALFUNCTIONING system
   - Words like: "not working", "error", "can't login", "access denied", "password reset",
     "system is down", "unable to export", "data is wrong", "records not displaying",
     "can't save", "bug", "malfunction", "configuration problem", "troubleshooting"
   - The email is about the firm's OWN internal systems failing
   → Use: IT Support (for internal tools, email, access) or CRM Issues (for CRM-specific bugs)
   → IMPORTANT: "CRM is not working" = CRM Issues. "Can't login to email" = IT Support.

C. ONE-KEYWORD OVERRIDE IS FORBIDDEN:
   - The word "CRM" alone MUST NOT classify an email as IT Support or CRM Issues.
   - The word "software" or "system" alone MUST NOT classify as IT Support.
   - The word "proposal" alone MUST NOT classify as Marketing & BD if the email is about a technical proposal review error.
   - Always use the FULL semantic context — subject, body, intent, customer_name, project_name together.

D. QUICK DISAMBIGUATION EXAMPLES:
   "New client wants CRM implementation, budget BHD 15,000"
   → SALES INTENT → Marketing & Business Development → Proposal Request → New Proposal

   "Client is seeking ERP software for their warehouse"
   → SALES INTENT → Marketing & Business Development → Proposal Request → New Proposal

   "CRM is not loading, records are missing"
   → TECHNICAL ISSUE → CRM Issues → (match appropriate subject from hierarchy)

   "Unable to log into email, password expired"
   → TECHNICAL ISSUE → IT Support → (match appropriate subject from hierarchy)

   "Please prepare engagement letter for audit"
   → BUSINESS DEVELOPMENT → Marketing & Business Development → EL Request → EL

   "Invoice for last quarter has not been processed"
   → Finance → Payable Management → Others (or relevant query)

STEP 2 — After determining INTENT, select the EXACT matching hierarchy:
- Match Request Type first, then Subject under that Request Type, then Query under that Subject.
- Names must match EXACTLY as listed in the hierarchy above.
- If a level cannot be confidently matched, return null for that level and deeper levels.
- Never guess or invent hierarchy entries.

Fields to extract for General Query classification:
- gq_request_type: Exactly match a Request Type name from the hierarchy above, or null if uncertain.
- gq_subject: Exactly match a Subject under that Request Type, or null if uncertain.
- gq_query: Exactly match a Query under that Subject, or null if uncertain.
- gq_request_type_confidence: Integer (0-100) confidence in Request Type.
- gq_subject_confidence: Integer (0-100) confidence in Subject.
- gq_query_confidence: Integer (0-100) confidence in Query.
- gq_priority_confidence: Integer (0-100) confidence in Priority.

STRICT CONSTRAINTS:
- Do NOT invent Request Types, Subjects, or Queries.
- Do NOT invent IDs.
- Semantic understanding is required; match implied requests (e.g. "account statement" -> Receivables Management -> SOA).
- If uncertain, return null.

Return ONLY valid JSON with these exact keys:
intent, secondary_intent, task_title, project_name, customer_name, contact_name, contact_phone, service_line_hint, task_description, sender_name, sender_designation, due_date, priority, task_tag, invoice_amount, confidence_score, confidence_level, gq_request_type, gq_subject, gq_query, gq_request_type_confidence, gq_subject_confidence, gq_query_confidence, gq_priority_confidence

Email Text:
{text}"""

    # 1. Process attachments
    attachments = attachments or []
    pdf_text_blocks = []
    image_contents = []

    with open("pdf_debug.log", "a") as f:
        f.write(f"\n--- New Request ---\nReceived {len(attachments)} attachments.\n")

    for att in attachments:
        ct = (att.get("contentType") or "").lower()
        content_bytes = att.get("contentBytes", "")
        att_name = (att.get("name") or "").lower()
        
        with open("pdf_debug.log", "a") as f:
            f.write(f"Att: {att_name}, CT: {ct}, Size: {len(content_bytes)}\n")
            
        if not content_bytes:
            continue
            
        if "pdf" in ct or att_name.endswith(".pdf"):
            pdf_text = extract_text_from_pdf_base64(content_bytes)
            if pdf_text:
                pdf_text_blocks.append(f"=== ATTACHMENT: {att.get('name', 'PDF')} ===\n{pdf_text}")
        elif "wordprocessingml" in ct or att_name.endswith(".docx"):
            docx_text = extract_text_from_docx_base64(content_bytes)
            if docx_text:
                pdf_text_blocks.append(f"=== ATTACHMENT: {att.get('name', 'DOCX')} ===\n{docx_text}")
        elif "image" in ct:
            image_contents.append({
                "type": "image_url",
                "image_url": {"url": f"data:{ct};base64,{content_bytes}"}
            })

    if pdf_text_blocks:
        prompt += "\n\n" + "\n\n".join(pdf_text_blocks)

    # 0. Local PII Pseudonymization & Anonymization (Zero-leakage local pre-processor)
    from agent.pseudonymizer import mask_email_text, unmask_data
    masked_prompt, token_mapping = mask_email_text(prompt)

    # -------------------------------------------------------------
    # Azure OpenAI / OpenAI Enterprise Switch (Uncomment to use)
    # -------------------------------------------------------------
    # from openai import AzureOpenAI
    # client = AzureOpenAI(
    #     api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
    #     api_version="2024-02-15-preview",
    #     azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT")
    # )
    # model_name = "gpt-4o" # or whatever your Azure deployment name is
    #
    # kwargs = {
    #     "messages": messages,
    #     "model": model_name,
    #     "temperature": 0.0,
    #     "response_format": {"type": "json_object"} # GPT-4o vision supports this
    # }
    # -------------------------------------------------------------

    # 2. Build Langchain payload (with safe prompt truncation to prevent Groq 413 TPM rate limits)
    from config.llm_factory import get_llm
    from langchain_core.messages import SystemMessage, HumanMessage

    safe_prompt = masked_prompt
    if len(safe_prompt) > 4500:
        safe_prompt = safe_prompt[:3000] + "\n...[Middle content truncated to comply with model token limits]...\n" + safe_prompt[-1200:]
        
    user_content = [{"type": "text", "text": safe_prompt}]
    if image_contents:
        user_content.extend(image_contents)
        
    messages = [
        SystemMessage(content="You are an expert CRM AI assistant. Extract structured CRM data from the input email, synthesize a concise 2-3 sentence executive summary for task_description, and respond ONLY with a valid JSON object matching the requested schema. CRITICAL: Do NOT output any <think> tags, chain-of-thought, or reasoning blocks. Your response MUST start IMMEDIATELY with '{' on line 1."),
        HumanMessage(content=user_content if image_contents else safe_prompt)
    ]
    
    is_vision = bool(image_contents)
    try:
        llm = get_llm(temperature=0.0, max_tokens=3072, is_vision=is_vision)
        
        # Try to enforce JSON mode if supported (avoid for Groq Qwen/DeepSeek which output <think> tags)
        provider_name = os.getenv("LLM_PROVIDER", "").lower()
        model_name = getattr(llm, "model_name", "").lower()
        if not is_vision and hasattr(llm, "bind") and provider_name != "groq":
            try:
                llm = llm.bind(response_format={"type": "json_object"})
            except Exception:
                pass
                
        # Print & log exact prompt sent to cloud AI for security verification
        print("\n" + "="*70)
        print("[SECURITY AUDIT] MASKED EMAIL TEXT SENT TO EXTERNAL CLOUD AI (Groq):")
        print("-" * 70)
        email_text_only = safe_prompt.split("Email Text:")[-1].strip() if "Email Text:" in safe_prompt else safe_prompt
        try:
            print(email_text_only)
        except Exception:
            pass
        print("="*70 + "\n")

        try:
            response = llm.invoke(messages)
        except Exception as inv_err:
            err_str = str(inv_err).lower()
            if "rate_limit_exceeded" in err_str or "413" in err_str or "tpm" in err_str or "too large" in err_str:
                print(f"[email_parser] TPM rate limit hit ({inv_err}), retrying with truncated prompt...")
                trunc_prompt = masked_prompt[:3000] + "\n...[Content truncated for token limits]..."
                trunc_user_content = [{"type": "text", "text": trunc_prompt}]
                if image_contents:
                    trunc_user_content.extend(image_contents)
                trunc_messages = [
                    SystemMessage(content="You are an expert CRM AI assistant. Extract structured CRM data from the input email, synthesize a concise 2-3 sentence executive summary for task_description, and respond ONLY with a valid JSON object matching the requested schema. CRITICAL: Do NOT output any <think> tags, chain-of-thought, or reasoning blocks. Your response MUST start IMMEDIATELY with '{' on line 1."),
                    HumanMessage(content=trunc_user_content if image_contents else trunc_prompt)
                ]
                plain_llm = get_llm(temperature=0.0, is_vision=is_vision)
                response = plain_llm.invoke(trunc_messages)
            elif "json_validate_failed" in err_str or "400" in err_str:
                print(f"[email_parser] JSON mode bind failed ({inv_err}), retrying with un-bound LLM...")
                plain_llm = get_llm(temperature=0.0, is_vision=is_vision)
                response = plain_llm.invoke(messages)
            else:
                raise inv_err
        
        # --- Token Tracking Metadata ---
        meta_dict = {}
        try:
            in_tok = 0
            out_tok = 0
            tot_tok = 0
            
            # A) Try usage_metadata (dict or object)
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                um = response.usage_metadata
                if isinstance(um, dict):
                    in_tok = um.get('input_tokens', 0) or um.get('prompt_tokens', 0)
                    out_tok = um.get('output_tokens', 0) or um.get('completion_tokens', 0)
                    tot_tok = um.get('total_tokens', in_tok + out_tok)
                else:
                    in_tok = getattr(um, 'input_tokens', getattr(um, 'prompt_tokens', 0))
                    out_tok = getattr(um, 'output_tokens', getattr(um, 'completion_tokens', 0))
                    tot_tok = getattr(um, 'total_tokens', in_tok + out_tok)

            # B) Fallback to response_metadata
            if not in_tok and hasattr(response, 'response_metadata') and isinstance(response.response_metadata, dict):
                rm = response.response_metadata
                usage = rm.get("token_usage") or rm.get("usage") or rm.get("tokenUsage") or rm.get("token_counts") or {}
                if isinstance(usage, dict):
                    in_tok = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                    out_tok = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
                    tot_tok = usage.get("total_tokens", in_tok + out_tok)

            # C) Fallback estimation if provider suppresses usage metadata when using llm.bind()
            if not in_tok:
                prompt_str = str(messages)
                completion_str = str(getattr(response, 'content', ''))
                in_tok = max(1, len(prompt_str) // 4)
                out_tok = max(1, len(completion_str) // 4)
                tot_tok = in_tok + out_tok

            tot_tok = max(tot_tok, in_tok + out_tok)

            # Model Name Resolution
            model_name_used = (
                getattr(llm, 'model_name', None) or 
                getattr(llm, 'model', None) or 
                getattr(getattr(llm, 'bound', None), 'model_name', None) or 
                getattr(getattr(llm, 'bound', None), 'model', None)
            )
            if not model_name_used or model_name_used == 'unknown':
                if hasattr(response, 'response_metadata') and isinstance(response.response_metadata, dict):
                    model_name_used = response.response_metadata.get("model_name") or response.response_metadata.get("model")
            if not model_name_used or model_name_used == 'unknown':
                model_name_used = os.getenv("VISION_MODEL", "llama-3.2-90b-vision-preview") if is_vision else (os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "qwen/qwen3.6-27b")

            has_att = len(attachments) > 0 if attachments else False
            ext = None
            if has_att and isinstance(attachments, list):
                first_name = str(attachments[0].get("name", ""))
                if "." in first_name:
                    ext = "." + first_name.split(".")[-1].lower()
                    
            from db.database import calculate_llm_cost
            cost = calculate_llm_cost(model_name_used, in_tok, out_tok)

            meta_dict = {
                "model_name": model_name_used,
                "total_attachments": len(attachments) if attachments else 0,
                "parsed_attachments": len(attachments) if attachments else 0,
                "token_tracking": {
                    "employee_id": employee_id,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "total_tokens": tot_tok,
                    "total_cost_usd": cost,
                    "has_attachment": has_att,
                    "file_extension": ext
                }
            }
        except Exception as te:
            print(f"Token tracking error: {te}")
        # ----------------------
        
        content_str = str(response.content or "").strip()
        parsed = clean_and_parse_json(content_str)
        
        print("[DEBUG EMAIL_PARSER] Cleaned content_str length:", len(content_str))
        try:
            print("[DEBUG EMAIL_PARSER] content_str snippet:", repr(content_str[:200]))
        except Exception:
            pass

        if not parsed:
            print("[EMAIL_PARSER WARNING] Failed to parse JSON from LLM output, applying fallback structure...")
            clean_summary_text = re.sub(r'(?i)(?:from|to|sent|date|subject|cc|bcc):\s*[^\n]+', '', text)
            clean_summary_text = re.sub(r'\s+', ' ', clean_summary_text).strip()
            parsed = {
                "intent": "Service Lead" if "prospective" in text.lower() or "client" in text.lower() else "General Task",
                "task_title": "Email Inquiry Task",
                "project_name": None,
                "customer_name": None,
                "contact_name": None,
                "contact_phone": None,
                "task_description": clean_summary_text[:300] if clean_summary_text else "Client inquiry requiring review.",
                "confidence_score": 60,
                "confidence_level": "medium"
            }
        
        # 0b. Local Unmasking: Restore real names, numbers, amounts in JSON result
        parsed = unmask_data(parsed, token_mapping)
        
        # Clean any unmasked residual token strings e.g. <AMOUNT_TOKEN_2>, <CUSTOMER_TOKEN_1>, <amount >, <amount_1>
        def _clean_residual_placeholders(obj: Any) -> Any:
            if isinstance(obj, str):
                cleaned = re.sub(r'<[\s]*[a-zA-Z0-9_\s-]+[\s]*>', '', obj, flags=re.IGNORECASE)
                cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
                return cleaned
            elif isinstance(obj, dict):
                return {k: _clean_residual_placeholders(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_clean_residual_placeholders(item) for item in obj]
            return obj

        parsed = _clean_residual_placeholders(parsed)
        
        # Define invalid phrase blacklist for entity extraction
        INVALID_CUSTOMER_PHRASES = [
            "information for your", "contact information", "further take up", 
            "see below", "please see", "thanks & regards", "kind regards", "best regards",
            "information for", "take up", "regards"
        ]

        # 1. Sanitize existing contact_name and customer_name if they contain invalid filler phrases
        for field in ["customer_name", "contact_name"]:
            val = parsed.get(field)
            if val:
                v_str = str(val).strip()
                if any(p in v_str.lower() for p in INVALID_CUSTOMER_PHRASES) or len(v_str) > 40:
                    parsed[field] = None

        # 2. Direct Salutation Regex Fallback (e.g. Mr. Usama, Ms. Jane)
        if not parsed.get("contact_name"):
            sal_m = re.search(r'\b(Mr\.|Mrs\.|Ms\.|Dr\.)\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)', text)
            if sal_m:
                parsed["contact_name"] = f"{sal_m.group(1)} {sal_m.group(2)}".strip()
            else:
                name_m = re.search(r'(?:client contact|contact person|prospect contact|reach out to|contact)\s+(?:is\s+)?(Mr\.|Mrs\.|Ms\.|Dr\.)?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})', text, re.IGNORECASE)
                if name_m:
                    cand = ((name_m.group(1) + " ") if name_m.group(1) else "") + name_m.group(2)
                    if not any(p in cand.lower() for p in INVALID_CUSTOMER_PHRASES):
                        parsed["contact_name"] = cand

        # 3. Dynamic Phone Extraction Fallback (no hardcoded names)
        if not parsed.get("contact_phone"):
            c_name = parsed.get("contact_name") or ""
            clean_first_name = re.sub(r'^(Mr\.|Mrs\.|Ms\.|Dr\.)\s*', '', c_name, flags=re.IGNORECASE).strip().split()[0] if c_name else ""
            
            keywords = ["contact", "mobile", "tel", "phone", "call", "cell", "reach", "number", "m", "t"]
            if clean_first_name and len(clean_first_name) > 2:
                keywords.insert(0, re.escape(clean_first_name))
            
            kw_pattern = "|".join(keywords)
            phone_m = re.search(fr'(?:{kw_pattern})[^\d]*\b(\+?\d{{1,4}}[-\s]*)?(\d{{7,14}})\b', text, re.IGNORECASE)
            if phone_m:
                parsed["contact_phone"] = (phone_m.group(1) or "") + phone_m.group(2)
            else:
                gen_phone = re.search(r'\b(?:\+?\d{1,4}[-\s]*)?\d{7,14}\b', text)
                if gen_phone:
                    parsed["contact_phone"] = gen_phone.group(0)

        # 4. Re-verify sanitization after fallbacks
        for field in ["customer_name", "contact_name"]:
            val = parsed.get(field)
            if val:
                v_str = str(val).strip()
                if any(p in v_str.lower() for p in INVALID_CUSTOMER_PHRASES) or len(v_str) > 40:
                    parsed[field] = None

        # 5. Fallback for new customers where LLM refuses to put a person's name as a company name
        if not parsed.get("customer_name") and parsed.get("contact_name"):
            parsed["customer_name"] = parsed["contact_name"]

        # Post-processing safeguard for Rectified / Sent By Mistake Resolution Notices
        rectified_phrases = ["rectified that", "sent by mistake", "already resolved", "mistake has been rectified", "issue is fixed", "fixed the issue"]
        if any(phrase in text.lower() for phrase in rectified_phrases):
            parsed["intent"] = "General Task"
            parsed["task_tag"] = "Resolution Notice"
            curr_desc = str(parsed.get("task_description") or "")
            if any(w in curr_desc.lower() for w in ["investigate", "fix the missing", "lacks proposal", "action required"]):
                parsed["task_description"] = "Sender confirms that the proposal issue has been rectified and resolved. Informational update only; no further action required."

        # 0c. Backend DB Verification — Cases 1 to 5
        try:
            parsed = enrich_email_with_db_cases(parsed, employee_id=employee_id)
        except Exception as e_db:
            print(f"[email_parser] enrich_email_with_db_cases error: {e_db}")

        # Parse confidence score & level
        try:
            conf_score_val = int(parsed.get("confidence_score", 90))
        except (TypeError, ValueError):
            conf_score_val = 90
        conf_level_val = str(parsed.get("confidence_level", "high")).lower()
        proc_time_ms = int((time.time() - start_time) * 1000)

        # Directly log telemetry and ML training dataset samples
        try:
            from db.database import save_ai_email_parsing_async, save_email_ml_dataset_async
            import asyncio
            
            import urllib.parse
            emp_id = employee_id if (employee_id and employee_id != 0) else None
            ref_str = None
            if reference_id and str(reference_id).strip() and str(reference_id).strip().lower() not in ("none", "null", "undefined"):
                ref_str = str(reference_id).strip()[:255]

            asyncio.create_task(save_ai_email_parsing_async(
                employee_id=emp_id,
                document_type="email_task",
                reference_id=ref_str,
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=tot_tok,
                total_cost_usd=cost,
                model_name=model_name_used,
                has_attachment=has_att,
                file_extension=ext,
                confidence_score=conf_score_val,
                confidence_level=conf_level_val,
                processing_status="PENDING",
                processing_time_ms=proc_time_ms
            ))

            # Extract keywords and thread statistics for ML dataset
            kw_set = set()
            for k_m in re.findall(r'(?:BHD|USD|\$|EUR|BD)\s*\d+(?:\.\d+)?', text, re.IGNORECASE): kw_set.add(k_m)
            for k_p in re.findall(r'\b\d{7,10}\b', text): kw_set.add(k_p)
            for k_a in re.findall(r'\b(?:proposal|invoice|audit|tax|engagement|estimation|leave|support|lead|chart of accounts|mis|reporting)\b', text, re.IGNORECASE): kw_set.add(k_a.lower())
            for k_n in re.findall(r'(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?', text): kw_set.add(k_n)
            
            thread_cnt = max(1, len(re.findall(r'From:\s', text, re.IGNORECASE)) + len(re.findall(r'Original Message', text, re.IGNORECASE)))
            
            # Robust extraction & sanitization for sender_email, to_emails, subject
            sender_e = sender_email
            if not sender_e or "subject:" in str(sender_e).lower():
                m_from = re.search(r'From:\s*([^\s\n<>]+@[^\s\n<>]+)', text, re.IGNORECASE)
                sender_e = m_from.group(1) if m_from else None
                
            to_e = to_emails
            if not to_e or "subject:" in str(to_e).lower():
                m_to = re.search(r'To:\s*([\w\.-]+@[\w\.-]+)', text, re.IGNORECASE)
                if m_to:
                    to_e = m_to.group(1).strip()
                else:
                    # Extract email addresses from to_emails string if present
                    extracted_addrs = re.findall(r'[\w\.-]+@[\w\.-]+', str(to_emails or ""))
                    to_e = ", ".join(extracted_addrs) if extracted_addrs else None
                
            subj_str = subject
            if subj_str:
                subj_str = re.sub(r'^(?:subject:\s*)+', '', str(subj_str), flags=re.IGNORECASE).strip()
            if not subj_str:
                m_subj = re.search(r'Subject:\s*([^\n]+)', text, re.IGNORECASE)
                if m_subj:
                    subj_str = re.sub(r'^(?:subject:\s*)+', '', m_subj.group(1), flags=re.IGNORECASE).strip()

            asyncio.create_task(save_email_ml_dataset_async(
                reference_id=ref_str,
                sender_email=sender_e,
                to_emails=to_e,
                subject=subj_str,
                body_clean=text,
                thread_count=thread_cnt,
                is_forwarded=parsed.get("isForwarded", False),
                forwarded_by_email=parsed.get("forwarderEmail"),
                predicted_intent=parsed.get("intent"),
                extracted_keywords=list(kw_set),
                extracted_entities=parsed,
                confidence_score=conf_score_val,
                employee_id=emp_id
            ))
        except Exception as dbe:
            print(f"Direct email_task & ML telemetry logging error: {dbe}")
            
        if isinstance(parsed, dict):
            parsed = evaluate_manual_review_conditions(parsed)
            enrichments = build_general_query_mapping(parsed)
            parsed["general_query_mapping"] = enrichments["general_query_mapping"]
            parsed["redirection_prompt"] = enrichments["redirection_prompt"]
            parsed["notification_badge"] = enrichments["notification_badge"]
            if meta_dict:
                parsed["_meta"] = meta_dict

        return parsed
    except Exception as e:
        print(f"Extraction error: {e}")
        return {}


def get_current_financial_year() -> dict:
    """
    Dynamically calculates current Financial Year for Grant Thornton corporate accounting standards.
    GT FY runs July 1 to June 30.
    Returns formatted strings e.g. {"fy_name": "FY2526", "fy_full": "2025-2026", "valid_formats": [...]}
    """
    now = datetime.datetime.now()
    year = now.year
    month = now.month

    if month >= 7:
        start_year = year
        end_year = year + 1
    else:
        start_year = year - 1
        end_year = year

    y1_str = str(start_year)
    y2_str = str(end_year)
    short_y2 = y2_str[-2:]

    return {
        "fy_name": f"FY{y1_str[-2:]}{short_y2}",
        "fy_full": f"{y1_str}-{y2_str}",
        "start_year": start_year,
        "end_year": end_year,
        "valid_formats": [
            f"{y1_str}-{y2_str}",
            f"{y1_str}-{short_y2}",
            f"FY{y1_str[-2:]}{short_y2}",
            y1_str,
            y2_str
        ]
    }


def enrich_email_with_db_cases(parsed: dict, employee_id: int = 0) -> dict:
    """
    Backend MySQL verification for the 5 Email-to-Task business cases & Re-Engagement.
    Matches Priority: Explicit Reference / Exact Project ID > Email/Domain > Exact Name > Partial Name Match.

    Case 1: Registered customer found -> set db_customer_id, db_customer_registered
    Case 2: Existing customer, new service line -> set intent=Service Lead, new_service_line=True
    Case 3: Existing customer, existing service line -> Current FY Running Project Follow-Up
    Case 4: Proposal/Engagement Letter request -> fetch latest proposal from `proposal` table
    Case 5: Invoice intent -> fetch latest invoice from `invoice` table (including status)
    Re-Engagement: Existing customer + existing service line + NO current FY project -> Re-Engagement Service Lead
    """
    if not isinstance(parsed, dict):
        return parsed

    current_fy = get_current_financial_year()
    parsed["current_financial_year"] = current_fy["fy_full"]
    parsed["customer_lookup_status"] = "UNCHECKED"

    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()

        cust_name = (parsed.get("customer_name") or "").strip()
        sender_email = (parsed.get("originalFromEmail") or parsed.get("sender_email") or "").strip()
        intent_raw = str(parsed.get("intent") or "").lower()
        desc_raw = str(parsed.get("task_description") or "").lower()
        project_hint = str(parsed.get("project_name") or "").strip()

        # Exclude internal firm network names from external client customer matching
        INTERNAL_FIRM_KEYWORDS = [
            "grant thornton", "gt bahrain", "gt oman", "gt uae", "gt kuwait", "gt qatar", 
            "gt saudi", "grant thornton oman", "grant thornton bahrain", "grant thornton uae",
            "grant thornton kuwait", "grant thornton qatar", "grant thornton saudi"
        ]
        if cust_name and any(kw in cust_name.lower() for kw in INTERNAL_FIRM_KEYWORDS):
            print(f"[enrich_db_cases] Excluding internal firm name '{cust_name}' from external customer database matching.")
            cust_name = ""
            parsed["customer_name"] = None

        with engine.connect() as conn:
            # ── Step 0: Check Employee Service Line for 98% Rule Priority ──────
            emp_service_line_id = None
            if employee_id and employee_id != 0:
                emp_sl_row = conn.execute(
                    text("SELECT emp_department_id FROM employees WHERE id = :eid LIMIT 1"),
                    {"eid": employee_id}
                ).fetchone()
                if emp_sl_row:
                    emp_service_line_id = emp_sl_row[0]

            # ── Step 1: Customer Lookup Hierarchy & Multiple Matches Check ─────
            customer_id = None
            matched_customers = []

            # 1a. Priority Match: Domain / Email search in customers table
            if sender_email and "@" in sender_email and not sender_email.endswith("@bh.gt.com"):
                domain = sender_email.split("@")[-1].lower()
                c_rows = conn.execute(
                    text("SELECT id, customer_name FROM customers WHERE (LOWER(email) LIKE :e OR LOWER(website) LIKE :d) AND is_active = 1"),
                    {"e": f"%{sender_email.lower()}%", "d": f"%{domain}%"}
                ).fetchall()
                if c_rows:
                    matched_customers = [{"id": r[0], "name": r[1]} for r in c_rows]

            # 1b. Priority Match: Exact Customer Name Match
            if not matched_customers and cust_name and len(cust_name) > 2:
                c_rows = conn.execute(
                    text("SELECT id, customer_name FROM customers WHERE LOWER(customer_name) = :name AND is_active = 1"),
                    {"name": cust_name.lower()}
                ).fetchall()
                if c_rows:
                    matched_customers = [{"id": r[0], "name": r[1]} for r in c_rows]

            # 1c. Priority Match: Partial Name Match
            if not matched_customers and cust_name and len(cust_name) > 2:
                c_rows = conn.execute(
                    text("SELECT id, customer_name FROM customers WHERE LOWER(customer_name) LIKE :name AND is_active = 1 LIMIT 5"),
                    {"name": f"%{cust_name.lower()}%"}
                ).fetchall()
                if c_rows:
                    matched_customers = [{"id": r[0], "name": r[1]} for r in c_rows]

            # Handle Customer Lookup Status
            if len(matched_customers) == 1:
                customer_id = matched_customers[0]["id"]
                parsed["db_customer_id"] = customer_id
                parsed["db_customer_name"] = matched_customers[0]["name"]
                parsed["db_customer_registered"] = True
                parsed["customer_lookup_status"] = "FOUND"
                parsed["multiple_customer_matches"] = False
                print(f"[enrich_db_cases] Customer single match found — id={customer_id}, name={matched_customers[0]['name']}")
            elif len(matched_customers) > 1:
                parsed["db_customer_registered"] = False
                parsed["customer_lookup_status"] = "MULTIPLE_MATCHES"
                parsed["multiple_customer_matches"] = True
                parsed["matched_customers_list"] = matched_customers
                print(f"[enrich_db_cases] Multiple customer matches found ({len(matched_customers)} candidates)")
            else:
                parsed["db_customer_registered"] = False
                parsed["customer_lookup_status"] = "NOT_FOUND" if cust_name else "NO_CUSTOMER_SPECIFIED"
                parsed["multiple_customer_matches"] = False

            # ── Step 2: Explicit Project Reference / Exact Project ID Check ────
            explicit_proj_row = None
            if project_hint and len(project_hint) > 2:
                # Check exact project ID or exact project name
                proj_by_id = conn.execute(
                    text("SELECT id, name, main_incharge, manager, service_line_id, created_at FROM projects WHERE (LOWER(name) LIKE :pname OR LOWER(id) = :pid) AND is_active = 1 LIMIT 1"),
                    {"pname": f"%{project_hint.lower()}%", "pid": project_hint.lower()}
                ).fetchone()
                if proj_by_id:
                    explicit_proj_row = proj_by_id
                    parsed["matched_explicit_project"] = True
                else:
                    # Explicit project mentioned in text but not found in CRM DB
                    if "prj-" in project_hint.lower() or "p-" in project_hint.lower() or len(project_hint) > 8:
                        parsed["explicit_project_not_found"] = True

            # ── Case 4: Proposal / Engagement Letter ───────────────────────────
            is_proposal_request = (
                "proposal" in intent_raw
                or "engagement" in intent_raw
                or "engagement letter" in desc_raw
                or "proposal" in desc_raw
            )
            if is_proposal_request:
                parsed["intent"] = "Service Lead"
                parsed["db_case"] = 4
                print(f"[enrich_db_cases] Case 4: Proposal/Engagement Letter intent detected")
                if customer_id:
                    p_row = conn.execute(
                        text(
                            "SELECT id, created_at FROM proposal "
                            "WHERE client_id = :cid AND is_active = 1 "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"cid": customer_id}
                    ).fetchone()
                    if p_row:
                        parsed["latest_proposal_date"] = str(p_row[1])
                        print(f"[enrich_db_cases] Case 4: Latest proposal date={p_row[1]}")

            # ── Case 5: Invoice Intent ─────────────────────────────────────────
            elif "invoice" in intent_raw or "billing" in desc_raw:
                parsed["intent"] = "Invoice"
                parsed["db_case"] = 5
                print(f"[enrich_db_cases] Case 5: Invoice intent detected")
                if customer_id:
                    inv_row = conn.execute(
                        text(
                            "SELECT id, invoice_no, total_amt_ex_vat, status, created_at FROM invoice "
                            "WHERE client_id = :cid AND is_active = 1 "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"cid": customer_id}
                    ).fetchone()
                    if inv_row:
                        parsed["latest_invoice_number"] = inv_row[1]
                        parsed["invoice_amount"] = str(inv_row[2])
                        parsed["latest_invoice_status"] = str(inv_row[3]) if inv_row[3] else "Pending"
                        parsed["latest_invoice_date"] = str(inv_row[4])
                        print(f"[enrich_db_cases] Case 5: Latest invoice no={inv_row[1]}, status={inv_row[3]}")

            # ── Cases 2, 3 & Re-Engagement: Existing Customer Service Line Check ─
            elif customer_id:
                sl_hint = (parsed.get("service_line_hint") or "").strip().lower()

                # Fetch all active projects for this customer
                proj_rows = conn.execute(
                    text(
                        "SELECT p.id, p.name, p.main_incharge, p.manager, p.service_line_id, p.created_at "
                        "FROM projects p WHERE p.client = :cid AND p.is_active = 1 "
                        "ORDER BY p.created_at DESC"
                    ),
                    {"cid": customer_id}
                ).fetchall()

                existing_sl_ids = {r[4] for r in proj_rows if r[4]}

                if sl_hint:
                    # Check if hinted service line exists in m_serviceline
                    sl_row = conn.execute(
                        text("SELECT id FROM m_serviceline WHERE LOWER(name) LIKE :h AND is_active = 1 LIMIT 1"),
                        {"h": f"%{sl_hint}%"}
                    ).fetchone()

                    if sl_row:
                        hinted_sl_id = sl_row[0]
                        if hinted_sl_id not in existing_sl_ids:
                            # Case 2: New service line for existing customer
                            parsed["intent"] = "Service Lead"
                            parsed["new_service_line"] = True
                            parsed["db_case"] = 2
                            print(f"[enrich_db_cases] Case 2: New service line detected sl_id={hinted_sl_id}")
                        else:
                            # Existing service line: Check for Current FY project vs Re-Engagement
                            candidate_projects = [p for p in proj_rows if p[4] == hinted_sl_id]
                            current_fy_projects = [
                                p for p in candidate_projects 
                                if p[5] and (str(p[5])[:4] in current_fy["valid_formats"] or str(p[5]) in current_fy["valid_formats"])
                            ]

                            if current_fy_projects:
                                # Case 3: Current FY Running Project Follow-Up
                                parsed["db_case"] = 3
                                parsed["is_reengagement"] = False
                                
                                # Prioritization ranking algorithm (Explicit > SL Match > 98% User Rule > Current FY > Recency)
                                def score_project(p):
                                    sc = 0
                                    if explicit_proj_row and p[0] == explicit_proj_row[0]: sc += 1000
                                    if p[4] == hinted_sl_id: sc += 500
                                    if emp_service_line_id and p[4] == emp_service_line_id: sc += 200
                                    if p[5] and str(p[5])[:4] in current_fy["valid_formats"]: sc += 100
                                    return sc

                                sorted_projs = sorted(current_fy_projects, key=score_project, reverse=True)
                                matched_proj = sorted_projs[0]

                                parsed["matched_project_id"] = matched_proj[0]
                                parsed["matched_project_name"] = matched_proj[1]
                                parsed["project_in_charge_id"] = matched_proj[2]
                                parsed["project_members"] = matched_proj[3]
                                parsed["financial_year"] = str(matched_proj[5])[:4] if matched_proj[5] else ""

                                # 98% Logged-In User Rule check
                                if emp_service_line_id and matched_proj[4] == emp_service_line_id:
                                    parsed["user_service_line_matched"] = True
                                    print(f"[enrich_db_cases] 98% Rule Satisfied: Selected project service line matches logged-in user service line ({emp_service_line_id})")
                                else:
                                    parsed["user_service_line_matched"] = False

                                print(f"[enrich_db_cases] Case 3: Current FY Running Project linked id={matched_proj[0]}")
                            else:
                                # RE-ENGAGEMENT: Existing service line, but NO project running in CURRENT FY
                                parsed["intent"] = "Service Lead"
                                parsed["is_reengagement"] = True
                                parsed["db_case"] = 2
                                parsed["reengagement_notice"] = "Re-Engagement: Existing customer with past experience in this service line, but no running project in Current FY."
                                
                                # Preserve past project reference for UI context without treating it as current running project
                                if candidate_projects:
                                    past_p = candidate_projects[0]
                                    parsed["past_project_reference"] = {
                                        "id": past_p[0],
                                        "name": past_p[1],
                                        "financial_year": str(past_p[5])[:4] if past_p[5] else ""
                                    }
                                print(f"[enrich_db_cases] RE-ENGAGEMENT DETECTED! Existing SL id={hinted_sl_id}, but 0 current FY projects.")
                    else:
                        parsed["db_case"] = 1
                else:
                    # No SL hint: link current FY project if available
                    current_fy_projs = [p for p in proj_rows if p[5] and str(p[5])[:4] in current_fy["valid_formats"]]
                    if current_fy_projs:
                        matched_proj = current_fy_projs[0]
                        parsed["matched_project_id"] = matched_proj[0]
                        parsed["matched_project_name"] = matched_proj[1]
                        parsed["project_in_charge_id"] = matched_proj[2]
                        parsed["project_members"] = matched_proj[3]
                    parsed["db_case"] = 1
                    print(f"[enrich_db_cases] Case 1: Registered customer, no SL hint")

    except Exception as dbe:
        parsed["customer_lookup_status"] = "LOOKUP_FAILED"
        parsed["db_customer_registered"] = False
        parsed["db_error_message"] = str(dbe)
        print(f"[enrich_email_with_db_cases] DB LOOKUP FAILED: {dbe}")

    return parsed


def evaluate_manual_review_conditions(parsed: dict) -> dict:
    """
    Evaluates Requirement 9: Manual Review Conditions across all specification triggers:
    - Insufficient extracted info (< 3 key fields)
    - Unverified Customer in CRM DB
    - DB Lookup Failure (DB Connection Error)
    - Multiple matches (Customers, Projects, Invoices)
    - Explicit Project ID mentioned in email not found in CRM
    - Low AI confidence score (< 70%)
    - Re-Engagement Notice

    Populates top-level fields:
    - requires_manual_review (bool)
    - manual_review_reasons (list)
    - manual_review_notice (str)
    """
    if not isinstance(parsed, dict):
        return parsed

    reasons = []

    # 1. Check DB lookup failure (DB Connection Error)
    if parsed.get("customer_lookup_status") == "LOOKUP_FAILED":
        reasons.append("CRM database lookup failed during verification (DB Connection Error). Manual verification required.")

    # 2. Check Customer DB verification
    customer = parsed.get("customer_name")
    if customer and parsed.get("db_customer_registered") is False and parsed.get("customer_lookup_status") != "LOOKUP_FAILED":
        reasons.append("Customer name could not be verified in the CRM database.")

    # 3. Check for multiple customer matches
    if parsed.get("multiple_customer_matches") or (isinstance(customer, list) and len(customer) > 1):
        reasons.append("Multiple customer matches found in CRM database. Manual selection required.")

    # 4. Check explicit project reference not found
    if parsed.get("explicit_project_not_found"):
        reasons.append("Explicit project reference mentioned in email was not found in CRM database.")

    # 5. Check verified field count (< 3 fields)
    project = parsed.get("project_name")
    intent = parsed.get("intent")
    desc = parsed.get("task_description")
    contact = parsed.get("contact_name")
    phone = parsed.get("contact_phone")
    service_line = parsed.get("service_line_hint")

    verified_field_count = 0
    if customer: verified_field_count += 1
    if project: verified_field_count += 1
    if intent and str(intent).strip().lower() not in ["general task", "none", ""]: verified_field_count += 1
    if desc and len(str(desc).strip()) > 10: verified_field_count += 1
    if contact: verified_field_count += 1
    if phone: verified_field_count += 1
    if service_line: verified_field_count += 1

    if verified_field_count < 3:
        reasons.append("Limited details extracted from email (fewer than 3 key fields matched).")

    # 6. Check confidence score / unclear intent
    try:
        conf_score = int(parsed.get("confidence_score", 90))
    except (TypeError, ValueError):
        conf_score = 90

    conf_level = str(parsed.get("confidence_level", "high")).lower()
    if conf_score < 70 or conf_level == "low":
        reasons.append(f"Low AI confidence ({conf_score}%). Intent requires manual verification.")

    requires_review = len(reasons) > 0
    parsed["requires_manual_review"] = requires_review
    parsed["manual_review_reasons"] = reasons

    if requires_review:
        parsed["manual_review_notice"] = (
            "Notice: Limited details were extracted from this email (fewer than 3 fields matched or unverified entities). "
            "Please verify and complete the query form manually."
        )
    elif parsed.get("is_reengagement"):
        parsed["manual_review_notice"] = "Notice: Re-Engagement Lead detected for existing customer (no running project in Current FY)."
    else:
        parsed["manual_review_notice"] = None

    return parsed


def build_general_query_mapping(parsed: dict) -> dict:
    """
    Dynamically classifies incoming email content into CRM General Query master data hierarchy:
    Request Type -> Subject -> Query (Issue)
    Using dynamic DB lookup, hierarchy validation, field-level confidence thresholds (0.85 default),
    and conditional entity resolution. Zero hardcoded if/elif classification rules.
    """
    CONFIDENCE_THRESHOLD = 0.85
    MEDIUM_THRESHOLD = 0.70

    # 1. Fetch dynamic master hierarchy from DB
    hierarchy = get_gq_master_hierarchy()

    # 2. Extract LLM raw hints and field-level confidence ratings
    raw_req_name = parsed.get("gq_request_type")
    raw_sub_name = parsed.get("gq_subject")
    raw_query_name = parsed.get("gq_query")

    def _clean_conf(val: Any, default: float = 0.90) -> float:
        if val is None:
            return default
        try:
            f = float(val)
            if f > 1.0:
                f = f / 100.0
            return max(0.0, min(1.0, f))
        except (ValueError, TypeError):
            return default

    conf_req_raw = _clean_conf(parsed.get("gq_request_type_confidence"), 0.90)
    conf_sub_raw = _clean_conf(parsed.get("gq_subject_confidence"), 0.88)
    conf_query_raw = _clean_conf(parsed.get("gq_query_confidence"), 0.85)
    conf_prio_raw = _clean_conf(parsed.get("gq_priority_confidence"), 0.90)

    # 3. Validate hierarchy against CRM master database
    verified_req, verified_sub, verified_query = validate_gq_hierarchy(
        raw_req_name, raw_sub_name, raw_query_name, hierarchy
    )

    reasons = list(parsed.get("manual_review_reasons") or [])

    # 4. Request Type Evaluation
    req_type_id = None
    req_type_name = None
    field_conf_req = conf_req_raw if verified_req else 0.0

    if not verified_req:
        reasons.append("Request Type could not be matched against CRM master data.")
    elif field_conf_req < CONFIDENCE_THRESHOLD and field_conf_req < MEDIUM_THRESHOLD:
        reasons.append(f"Request Type classification confidence ({int(field_conf_req*100)}%) is below threshold.")
    else:
        req_type_id = verified_req["id"]
        req_type_name = verified_req["name"]

    # 5. Subject Evaluation
    sub_id = None
    subject_name = None
    field_conf_sub = conf_sub_raw if (verified_sub and req_type_id) else 0.0

    if not req_type_id:
        pass  # Cannot populate Subject if Request Type failed
    elif not verified_sub:
        reasons.append("Subject could not be matched under selected Request Type.")
    elif field_conf_sub < CONFIDENCE_THRESHOLD and field_conf_sub < MEDIUM_THRESHOLD:
        reasons.append(f"Subject classification confidence ({int(field_conf_sub*100)}%) is below threshold.")
    else:
        sub_id = verified_sub["id"]
        subject_name = verified_sub["name"]

    # 6. Query Evaluation
    issue_id = None
    query_name = None
    field_conf_query = conf_query_raw if (verified_query and sub_id) else 0.0

    if not sub_id:
        pass  # Cannot populate Query if Subject failed
    elif not verified_query:
        reasons.append("Query could not be matched under selected Subject.")
    elif field_conf_query < CONFIDENCE_THRESHOLD and field_conf_query < MEDIUM_THRESHOLD:
        reasons.append(f"Query classification confidence ({int(field_conf_query*100)}%) is below threshold.")
    else:
        issue_id = verified_query["id"]
        query_name = verified_query["name"]

    # 7. Priority Resolution
    priority_raw = str(parsed.get("priority") or "Medium").strip().capitalize()
    if priority_raw not in ["Low", "Medium", "High"]:
        priority_raw = "Medium"
    field_conf_prio = conf_prio_raw

    # 8. Conditional Entity Resolution
    entities = resolve_general_query_entities(parsed, req_type_id, sub_id, issue_id)
    customer_id = entities.get("customer_id")
    customer_name = entities.get("customer_name")
    project_id = entities.get("project_id")
    proposal_id = entities.get("proposal_id")
    saleslead_id = entities.get("saleslead_id")

    # Evaluate Customer Confidence & Reasons
    cust_status = parsed.get("customer_lookup_status", "NOT_FOUND")
    if cust_status == "FOUND" and customer_id:
        field_conf_cust = 0.95
    elif cust_status == "MULTIPLE_MATCHES":
        field_conf_cust = 0.40
        reasons.append("Multiple customer matches found in CRM database. Manual selection required.")
    else:
        field_conf_cust = 0.50 if customer_name else None
        if customer_name and not customer_id:
            reasons.append("Customer name could not be verified in the CRM database.")

    # Evaluate Project Confidence & Reasons
    proj_status = entities.get("project_status", "NOT_APPLICABLE")
    if proj_status == "FOUND" and project_id:
        field_conf_proj = 0.95
    elif proj_status == "MULTIPLE_MATCHES":
        field_conf_proj = 0.40
        reasons.append("Multiple project matches found for this customer. Manual selection required.")
    elif proj_status == "NOT_FOUND":
        field_conf_proj = 0.50
        reasons.append("Specified project was not found in CRM database.")
    else:
        field_conf_proj = None

    # Evaluate Proposal Confidence & Reasons
    prop_status = entities.get("proposal_status", "NOT_APPLICABLE")
    if prop_status == "FOUND" and proposal_id:
        field_conf_prop = 0.95
    elif prop_status == "MULTIPLE_MATCHES":
        field_conf_prop = 0.40
        reasons.append("Multiple active proposals found for customer. Manual selection required.")
    elif prop_status == "NOT_FOUND":
        field_conf_prop = 0.50
    else:
        field_conf_prop = None

    # Evaluate Sales Lead Confidence & Reasons
    lead_status = entities.get("saleslead_status", "NOT_APPLICABLE")
    if lead_status == "FOUND" and saleslead_id:
        field_conf_lead = 0.95
    elif lead_status == "MULTIPLE_MATCHES":
        field_conf_lead = 0.40
        reasons.append("Multiple active sales leads found for customer. Manual selection required.")
    elif lead_status == "NOT_FOUND":
        field_conf_lead = 0.50
    else:
        field_conf_lead = None

    # 9. Verify Minimum Extracted Fields (< 3 fields filled)
    filled_fields_count = len([f for f in [req_type_id, sub_id, issue_id, customer_id, project_id, proposal_id, saleslead_id] if f is not None])
    if filled_fields_count < 3:
        reasons.append("Limited details extracted from email (fewer than 3 key CRM fields verified).")

    # Deduplicate reasons
    unique_reasons = []
    for r in reasons:
        if r not in unique_reasons:
            unique_reasons.append(r)

    manual_review_required = len(unique_reasons) > 0

    sub_detail = str(parsed.get("task_description") or "").strip()

    field_confidence = {
        "request_type": field_conf_req if req_type_id else (field_conf_req if field_conf_req > 0 else None),
        "subject": field_conf_sub if sub_id else (field_conf_sub if field_conf_sub > 0 else None),
        "query": field_conf_query if issue_id else (field_conf_query if field_conf_query > 0 else None),
        "priority": field_conf_prio,
        "customer": field_conf_cust,
        "project": field_conf_proj,
        "proposal": field_conf_prop,
        "service_lead": field_conf_lead
    }

    mapping = {
        "req_type_id": req_type_id,
        "req_type_name": req_type_name,
        "req_id": req_type_id,

        "sub_id": sub_id,
        "subject_name": subject_name,
        "subject_id": sub_id,

        "issue_id": issue_id,
        "query": query_name,

        "priority": priority_raw,
        "sub_detail": sub_detail,

        "project_id": project_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "proposal_id": proposal_id,
        "saleslead_id": saleslead_id,

        "field_confidence": field_confidence,
        "insufficient_info": manual_review_required,
        "insufficient_message": "Partial AI Auto-Fill: Some query fields could not be verified automatically. Please review and complete the remaining details manually." if manual_review_required else None,
        "manual_review_required": manual_review_required,
        "manual_review_reasons": unique_reasons
    }

    redirection_prompt = {
        "message": mapping["insufficient_message"] if manual_review_required else "Do you want to redirect to General Query for this task?",
        "options": {
            "yes": {
                "label": "Yes",
                "redirect_to": "/self-services/general-queries/add",
                "auto_fill_filters": True,
                "description": "Redirects to General Queries page with pre-filled AI chips & project dropdown."
            },
            "no": {
                "label": "No",
                "redirect_to": "/email-tasks",
                "existing_flow": True,
                "description": "Follows existing flow and lands on the Email Task section of the landing page."
            }
        }
    }

    notification_badge = {
        "has_new_task": True,
        "badge_type": "info",
        "badge_text": "New Task",
        "assigned_to": parsed.get("assigned_to")
    }

    return {
        "general_query_mapping": mapping,
        "redirection_prompt": redirection_prompt,
        "notification_badge": notification_badge
    }



