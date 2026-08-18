import json
import re
import os
import httpx
from pydantic import BaseModel
from typing import Optional

# ─── Pydantic Model (Matches exactly what Lambda returned) ────────────────────
class LeadExtraction(BaseModel):
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    budget_value: Optional[float] = None
    summary: Optional[str] = None
    serviceline_id: Optional[int] = None
    serviceline_name: Optional[str] = None
    servicetype_id: Optional[int] = None
    servicetype_name: Optional[str] = None
    sub_servicetype_id: Optional[int] = None
    sub_servicetype_name: Optional[str] = None
    industry_id: Optional[int] = None
    industry_name: Optional[str] = None

# ─── Aggressive name normalizer (from Lambda) ─────────────────────────────────
def norm_name(s: str) -> str:
    if not s:
        return ""
    # Remove punctuation: . - , & '
    s = re.sub(r"[.\-,&']", "", s.lower())
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s

def names_match(a: str, b: str) -> bool:
    na = norm_name(a)
    nb = norm_name(b)
    if not na or not nb or na == "unknown" or nb == "unknown":
        return False
    return na == nb or na in nb or nb in na

# ─── Lead Source logic ────────────────────────────────────────────────────────
def resolve_lead_source(sender_email: str) -> str:
    if not sender_email:
        return "external"
    parts = sender_email.split("@")
    if len(parts) > 1:
        domain = parts[1].lower().strip()
        if domain == "bh.gt.com":
            return "internal"
    return "external"

def clean_and_parse_json(text: str) -> dict:
    """Robust JSON parser that repairs common LLM glitches (Extra data, trailing commas, unescaped newlines, etc.)."""
    if not text or not text.strip():
        return {}

    # Strip markdown backticks
    cleaned_text = re.sub(r'```(?:json)?', '', text).replace('```', '').strip()

    # 1. Attempt raw_decode from the first '{' to handle 'Extra data' (e.g. trailing text or multiple JSON objects)
    first_brace = cleaned_text.find('{')
    if first_brace != -1:
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(cleaned_text[first_brace:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # 2. Try non-greedy block extraction
    matches = re.findall(r'\{[\s\S]*?\}', cleaned_text)
    for m in matches:
        try:
            return json.loads(m)
        except Exception:
            try:
                fixed = re.sub(r',\s*([\}\]])', r'\1', m)
                return json.loads(fixed)
            except Exception:
                continue

    # 3. Greedy match as fallback
    match = re.search(r'\{[\s\S]*\}', cleaned_text)
    if not match:
        return {}
    raw_json = match.group(0)

    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        pass

    # 4. Remove trailing commas before } or ]
    cleaned = re.sub(r',\s*([\}\]])', r'\1', raw_json)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 5. Escape literal unescaped newlines inside JSON strings
    try:
        def _fix_newlines(m):
            return m.group(0).replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        cleaned_nl = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', _fix_newlines, cleaned)
        return json.loads(cleaned_nl)
    except json.JSONDecodeError:
        pass

    # 6. Fallback: Python literal evaluation
    try:
        import ast
        val = ast.literal_eval(cleaned)
        if isinstance(val, dict):
            return val
    except Exception:
        pass

    return {}

# ─── CRM DB / API Fuzzy Matcher ──────────────────────────────────────────────
async def resolve_client_type(
    company_name: str,
    contact_name: str,
    contact_email: str
) -> dict:
    import asyncio
    debug_info = []

    def _db_lookup():
        n_email = (contact_email or "").lower().strip()
        n_comp = (company_name or "").strip()
        n_cont = (contact_name or "").strip()

        try:
            from db.database import get_db_engine
            from sqlalchemy import text
            engine = get_db_engine()
            with engine.connect() as conn:
                # 1. Search customer_contact_details by email
                if n_email:
                    row = conn.execute(
                        text("SELECT customer_id, id FROM customer_contact_details WHERE LOWER(TRIM(email_id)) = :email AND customer_id IS NOT NULL LIMIT 1"),
                        {"email": n_email}
                    ).fetchone()
                    if row:
                        return {"client_type": "existing", "customer_id": row[0], "contact_id": row[1], "debug": "Matched email in customer_contact_details"}

                    # Search contacts by email
                    row = conn.execute(
                        text("SELECT id, cd_company_name, first_name, last_name, email FROM contacts WHERE LOWER(TRIM(email)) = :email AND is_active = 1 LIMIT 1"),
                        {"email": n_email}
                    ).fetchone()
                    if row:
                        return {"client_type": "existing", "customer_id": None, "contact_id": row[0], "debug": "Matched email in contacts table"}

                # 2. Search customers table by email or company name
                if n_email:
                    row = conn.execute(
                        text("SELECT id FROM customers WHERE LOWER(TRIM(cust_email)) = :email AND is_active = 1 LIMIT 1"),
                        {"email": n_email}
                    ).fetchone()
                    if row:
                        return {"client_type": "existing", "customer_id": row[0], "contact_id": None, "debug": "Matched cust_email in customers table"}

                if n_comp and len(n_comp) > 2:
                    row = conn.execute(
                        text("SELECT id FROM customers WHERE LOWER(customer_name) LIKE :comp AND is_active = 1 LIMIT 1"),
                        {"comp": f"%{n_comp.lower()}%"}
                    ).fetchone()
                    if row:
                        return {"client_type": "existing", "customer_id": row[0], "contact_id": None, "debug": "Matched company_name in customers table"}

        except Exception as e:
            debug_info.append(f"DB lookup error: {str(e)}")

        return None

    # Try direct DB lookup
    db_res = await asyncio.to_thread(_db_lookup)
    if db_res:
        return db_res

    # If DB lookup finds no existing match, return as new client
    return {
        "client_type": "new",
        "customer_id": None,
        "contact_id": None,
        "debug": " | ".join(debug_info) if debug_info else "No existing client match found in DB"
    }

# ─── Master Data Helper ───────────────────────────────────────────────────────
def load_db_master_data_if_needed(context: dict) -> dict:
    """
    Ensure all master lists exist in context. If any are missing or empty,
    fetch them directly from the MySQL database.
    """
    ctx = dict(context or {})

    # Check key variations
    if 'service_lines' in ctx and 'serviceLines' not in ctx:
        ctx['serviceLines'] = ctx['service_lines']
    if 'industry_types' in ctx and 'industryTypes' not in ctx:
        ctx['industryTypes'] = ctx['industry_types']
    elif 'industries' in ctx and 'industryTypes' not in ctx:
        ctx['industryTypes'] = ctx['industries']
    if 'service_types' in ctx and 'serviceTypes' not in ctx:
        ctx['serviceTypes'] = ctx['service_types']
    if 'sub_service_types' in ctx and 'subServiceTypes' not in ctx:
        ctx['subServiceTypes'] = ctx['sub_service_types']

    has_sl = bool(ctx.get('serviceLines'))
    has_ind = bool(ctx.get('industryTypes'))
    has_st = bool(ctx.get('serviceTypes'))
    has_sst = bool(ctx.get('subServiceTypes'))

    if has_sl and has_ind and has_st and has_sst:
        return ctx

    # Auto-load missing lists from MySQL DB
    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        with engine.connect() as conn:
            if not has_sl:
                sl_rows = conn.execute(text("SELECT id, name FROM m_serviceline WHERE is_active = 1")).fetchall()
                ctx['serviceLines'] = [{"id": r[0], "name": r[1]} for r in sl_rows]

            if not has_ind:
                ind_rows = conn.execute(text("SELECT id, name FROM m_industry_type WHERE is_active = 1")).fetchall()
                ctx['industryTypes'] = [{"id": r[0], "name": r[1]} for r in ind_rows]

            if not has_st:
                st_rows = conn.execute(text("SELECT id, name, service_line_id FROM m_servicetype WHERE is_active = 1")).fetchall()
                ctx['serviceTypes'] = [{"id": r[0], "name": r[1], "service_line_id": r[2]} for r in st_rows]

            if not has_sst:
                sst_rows = conn.execute(text("SELECT id, name, service_type_id FROM m_sub_servicetype WHERE is_active = 1")).fetchall()
                ctx['subServiceTypes'] = [{"id": r[0], "name": r[1], "service_type_id": r[2]} for r in sst_rows]
    except Exception as e:
        print(f"[Lead Parser] Master Data Auto-load Error: {e}")

    return ctx

# ─── Main Extraction Method ───────────────────────────────────────────────────
async def extract_lead_from_email(
    subject: str,
    html_body: str,
    text_body: str,
    outer_from: str,
    outer_to: str,
    context: dict,
    employee_id: int
) -> dict:
    import asyncio
    import time
    from agent.email_parser import strip_html_to_text, parse_forwarded_email
    from config.llm_factory import get_llm
    from langchain_core.messages import HumanMessage, SystemMessage

    # 1. Clean HTML
    clean_text = strip_html_to_text(html_body) if html_body else text_body

    # 2. Parse Forwarded structure to get real sender
    parsed_email = parse_forwarded_email(subject, clean_text, outer_from, outer_to)
    is_forwarded = parsed_email.get("isForwarded", False)

    sender_email = parsed_email.get("originalFromEmail") if is_forwarded else outer_from
    sender_name  = parsed_email.get("originalFrom")      if is_forwarded else outer_from

    # Parse out email address if sender_email contains "<...>"
    if sender_email and "<" in sender_email and ">" in sender_email:
        m = re.search(r'<([^>]+)>', sender_email)
        if m:
            sender_email = m.group(1).strip()

    # 3. Auto-load master data from DB if context is missing/partial
    context = load_db_master_data_if_needed(context)

    sl_list  = "\n".join([f"ID {s.get('id')}: {s.get('name')}" for s in context.get('serviceLines', [])])
    ind_list = "\n".join([f"ID {i.get('id')}: {i.get('name')}" for i in context.get('industryTypes', [])])

    # 4. Build System Prompt 1
    system_prompt_1 = f"""You are an expert CRM analyst for Grant Thornton Bahrain. Extract ALL possible lead information from the email below.
SENDER (already known — use as fallback if not in email body):
- Sender Email: {sender_email}
- Sender Name: {sender_name}

CRITICAL: Use these specific CRM IDs for mapping:
SERVICE LINES:
{sl_list or "(none)"}

INDUSTRIES:
{ind_list or "(none)"}

Return ONLY a valid JSON object with these EXACT keys (no extra text before or after, no raw newlines inside string values):
{{
  "company_name": "Company or organization name, or null",
  "contact_name": "Full name of the person who sent the email",
  "contact_email": "Email address of the contact",
  "contact_phone": "Phone number as string (digits only), or null",
  "budget_value": null or a plain number (no currency symbols),
  "summary": "Brief 1-2 sentence summary of the inquiry (on a single line)",
  "serviceline_id": null or numeric ID from SERVICE LINES,
  "serviceline_name": "matching service line name or null",
  "industry_id": null or numeric ID from INDUSTRIES,
  "industry_name": "matching industry name or null",
  "confidence_score": "Calculate between 0-100. Start at 100. Deduct 11 if no contact phone. Deduct 14 if no budget mentioned. Deduct 8 if company name is unclear. Output exact integer.",
  "confidence_level": "High" (80-100), "Medium" (50-79), or "Low" (0-49)
}}"""

    user_prompt_1 = f"Subject: {subject}\n\nEmail Body:\n{clean_text[:3000]}"

    # 5. Call LLM
    llm = get_llm(temperature=0)
    start_time = time.time()

    # Token Tracking state
    in_tok = 0
    out_tok = 0

    def add_tokens(resp):
        nonlocal in_tok, out_tok
        if hasattr(resp, 'usage_metadata') and resp.usage_metadata:
            in_tok += resp.usage_metadata.get('input_tokens', 0)
            out_tok += resp.usage_metadata.get('output_tokens', 0)
        elif hasattr(resp, 'response_metadata') and resp.response_metadata:
            usage = resp.response_metadata.get("token_usage", {})
            if not usage and "usage" in resp.response_metadata:
                usage = resp.response_metadata["usage"]
            in_tok += usage.get("prompt_tokens", 0)
            out_tok += usage.get("completion_tokens", 0)

    try:
        # --- STEP 1: Top-Level Extraction ---
        response_1 = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=system_prompt_1), HumanMessage(content=user_prompt_1)]
        )
        add_tokens(response_1)
        raw_text_1 = response_1.content if hasattr(response_1, "content") else str(response_1)
        parsed = clean_and_parse_json(raw_text_1)

        # --- STEP 2: Deep-Dive Service Types (ONLY if Service Line is identified) ---
        sl_id = parsed.get("serviceline_id")
        if sl_id:
            # Filter Service Types list down to matching service line
            filtered_st = [
                st for st in context.get('serviceTypes', [])
                if str(st.get('service_line_id') or st.get('serviceLineId') or (st.get('serviceLine') if isinstance(st.get('serviceLine'), dict) else {}).get('id')) == str(sl_id)
            ]
            filtered_st_ids = [str(st.get('id')) for st in filtered_st]
            filtered_sst = [
                sst for sst in context.get('subServiceTypes', [])
                if str(sst.get('service_type_id') or sst.get('serviceTypeId') or (sst.get('serviceType') if isinstance(sst.get('serviceType'), dict) else {}).get('id')) in filtered_st_ids
            ]

            st_list_2 = "\n".join([f"ID {s.get('id')}: {s.get('name')}" for s in filtered_st])
            sst_list_2 = "\n".join([f"ID {s.get('id')}: {s.get('name')}" for s in filtered_sst])

            if filtered_st:
                system_prompt_2 = f"""You are a mapping expert. Based on the client inquiry summary, select the most relevant Service Type and Sub Service Type from the lists below.
Inquiry Summary: {parsed.get('summary', 'Unknown')}

SERVICE TYPES:
{st_list_2 or "(none)"}

SUB SERVICE TYPES (child of service types):
{sst_list_2 or "(none)"}

Return ONLY a valid JSON object with these EXACT keys:
{{
  "servicetype_id": null or numeric ID from SERVICE TYPES,
  "servicetype_name": "matching service type name or null",
  "sub_servicetype_id": null or numeric ID from SUB SERVICE TYPES,
  "sub_servicetype_name": "matching sub service type name or null"
}}"""
                response_2 = await asyncio.to_thread(
                    llm.invoke,
                    [SystemMessage(content=system_prompt_2), HumanMessage(content="Map the types now.")]
                )
                add_tokens(response_2)
                raw_text_2 = response_2.content if hasattr(response_2, "content") else str(response_2)
                parsed_2 = clean_and_parse_json(raw_text_2)
                if parsed_2:
                    parsed.update(parsed_2)

        extracted_obj = LeadExtraction(**{k: parsed.get(k) for k in LeadExtraction.model_fields})

    except Exception as e:
        print(f"[Lead Extraction] LLM Failed: {e}")
        raise RuntimeError(f"AI Extraction failed due to API limits or model error: {e}")

    # --- Cost & Analytics ---
    tot_tok = in_tok + out_tok
    cost = 0.0
    model_name_used = (
        getattr(llm, 'model_name', None) or 
        getattr(llm, 'model', None) or 
        getattr(getattr(llm, 'bound', None), 'model_name', None) or 
        getattr(getattr(llm, 'bound', None), 'model', None) or 
        os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "qwen/qwen3.6-27b"
    )

    from db.database import calculate_llm_cost
    cost = calculate_llm_cost(model_name_used, in_tok, out_tok)

    execution_time_ms = int((time.time() - start_time) * 1000)

    try:
        confidence_score_val = int(parsed.get("confidence_score", 0))
    except (TypeError, ValueError):
        confidence_score_val = 0
    confidence_level_val = str(parsed.get("confidence_level", "high")).lower()

    # 6. Save Telemetry — unified DB sink ai_email_parsing
    from db.database import save_ai_email_parsing_async
    asyncio.create_task(save_ai_email_parsing_async(
        employee_id=employee_id if employee_id != 0 else None,
        document_type="email_lead",
        reference_id=subject[:50],
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        total_cost_usd=cost,
        model_name=model_name_used,
        has_attachment=False,
        file_extension=None,
        confidence_score=int(confidence_score_val),
        confidence_level=confidence_level_val,
        processing_status="Success",
        processing_time_ms=execution_time_ms
    ))

    extracted_dict = extracted_obj.model_dump()

    # 7. Lead Source
    lead_source = resolve_lead_source(sender_email)

    # 8. Client Type (fuzzy match via DB/API)
    company = extracted_dict.get('company_name') or ""
    contact = extracted_dict.get('contact_name') or sender_name or ""
    c_email  = extracted_dict.get('contact_email') or sender_email or ""

    client_type_result = await resolve_client_type(company, contact, c_email)

    # 9. Final Response
    return {
        "company_name":         extracted_dict.get("company_name"),
        "contact_name":         extracted_dict.get("contact_name") or sender_name,
        "contact_email":        extracted_dict.get("contact_email") or sender_email,
        "contact_phone":        extracted_dict.get("contact_phone"),
        "budget_value":         extracted_dict.get("budget_value"),
        "summary":              extracted_dict.get("summary"),
        "serviceline_id":       extracted_dict.get("serviceline_id"),
        "serviceline_name":     extracted_dict.get("serviceline_name"),
        "servicetype_id":       extracted_dict.get("servicetype_id"),
        "servicetype_name":     extracted_dict.get("servicetype_name"),
        "sub_servicetype_id":   extracted_dict.get("sub_servicetype_id"),
        "sub_servicetype_name": extracted_dict.get("sub_servicetype_name"),
        "industry_id":          extracted_dict.get("industry_id"),
        "industry_name":        extracted_dict.get("industry_name"),
        "client_type":          client_type_result.get("client_type"),
        "customer_id":          client_type_result.get("customer_id"),
        "contact_id":           client_type_result.get("contact_id"),
        "lead_source":          lead_source,
        "debug_info":           client_type_result.get("debug")
    }

