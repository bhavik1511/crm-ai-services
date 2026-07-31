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

# ─── CRM API Fuzzy Matcher (from Lambda) ──────────────────────────────────────
async def resolve_client_type(
    company_name: str,
    contact_name: str,
    contact_email: str
) -> dict:
    debug_info = []

    crm_api_base = os.environ.get("CRM_API_BASE", "http://localhost:3001/api/v1")
    token = os.environ.get("CRM_JWT_SECRET", "dummy_token")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    n_email = (contact_email or "").lower().strip()
    is_existing = False
    found_customer_id = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── Check contacts table ─────────────────────────────────────────────
        try:
            c_resp = await client.get(f"{crm_api_base}/contact/all", headers=headers)
            if c_resp.status_code == 200:
                c_json = c_resp.json()
                contacts = c_json if isinstance(c_json, list) else (c_json.get('data') or c_json.get('rows') or [])
                debug_info.append(f"Fetched {len(contacts)} contacts")

                for c in contacts:
                    db_email = (c.get("email") or "").lower().strip()
                    db_full_name = f"{c.get('first_name') or ''} {c.get('last_name') or ''}".strip()
                    customer_obj = c.get("customer") or {}
                    db_company = c.get("cd_company_name") or customer_obj.get("customer_name") or ""

                    if (n_email and db_email and n_email == db_email) or \
                       names_match(contact_name, db_full_name) or \
                       names_match(company_name, db_company):

                        is_existing = True
                        if c.get("customer_id") or customer_obj.get("id"):
                            found_customer_id = c.get("customer_id") or customer_obj.get("id")
                            break
            else:
                debug_info.append(f"contact/all failed: {c_resp.status_code}")
        except Exception as e:
            debug_info.append(f"contact/all fetch error: {str(e)}")

        if is_existing and found_customer_id:
            return {"client_type": "existing", "customer_id": found_customer_id, "debug": " | ".join(debug_info)}

        # ── Check customers table ────────────────────────────────────────────
        try:
            k_resp = await client.get(f"{crm_api_base}/customer/all", headers=headers)
            if k_resp.status_code == 200:
                k_json = k_resp.json()
                customers = k_json if isinstance(k_json, list) else (k_json.get('data') or k_json.get('rows') or [])
                debug_info.append(f"Fetched {len(customers)} customers")

                for c in customers:
                    db_email = (c.get("cust_email") or "").lower().strip()
                    db_cust_name = c.get("customer_name") or ""

                    if (n_email and db_email and n_email == db_email) or \
                       names_match(company_name, db_cust_name) or \
                       names_match(contact_name, db_cust_name):

                        return {"client_type": "existing", "customer_id": c.get("id"), "debug": " | ".join(debug_info)}
            else:
                debug_info.append(f"customer/all failed: {k_resp.status_code}")
        except Exception as e:
            debug_info.append(f"customer/all fetch error: {str(e)}")

    debug_info.append("No matches found in DB")
    return {
        "client_type": "existing" if is_existing else "new",
        "customer_id": found_customer_id,
        "debug": " | ".join(debug_info)
    }

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
    from config.llm_factory import get_llm                    # ✅ correct import path
    from langchain_core.messages import HumanMessage, SystemMessage

    # 1. Clean HTML
    clean_text = strip_html_to_text(html_body) if html_body else text_body

    # 2. Parse Forwarded structure to get real sender
    parsed_email = parse_forwarded_email(subject, clean_text, outer_from, outer_to)
    is_forwarded = parsed_email.get("isForwarded", False)

    sender_email = parsed_email.get("originalFromEmail") if is_forwarded else outer_from
    sender_name  = parsed_email.get("originalFrom")      if is_forwarded else outer_from

    # 3. AI Context Lists (STEP 1: High-Level Taxonomy only)
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

Return ONLY a valid JSON object with these EXACT keys (no extra text before or after):
{{
  "company_name": "Company or organization name, or null",
  "contact_name": "Full name of the person who sent the email",
  "contact_email": "Email address of the contact",
  "contact_phone": "Phone number as string (digits only), or null",
  "budget_value": null or a plain number (no currency symbols),
  "summary": "Brief 1-2 sentence summary of the inquiry",
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
        json_match_1 = re.search(r'\{[\s\S]*\}', raw_text_1)
        parsed = json.loads(json_match_1.group(0)) if json_match_1 else {}

        # --- STEP 2: Deep-Dive Service Types (ONLY if Service Line is identified) ---
        sl_id = parsed.get("serviceline_id")
        if sl_id:
            # Filter huge Service Types list down to a few items
            filtered_st = [st for st in context.get('serviceTypes', []) if str(st.get('service_line_id')) == str(sl_id)]
            filtered_st_ids = [str(st.get('id')) for st in filtered_st]
            filtered_sst = [sst for sst in context.get('subServiceTypes', []) if str(sst.get('service_type_id')) in filtered_st_ids]

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
                json_match_2 = re.search(r'\{[\s\S]*\}', raw_text_2)
                if json_match_2:
                    parsed_2 = json.loads(json_match_2.group(0))
                    parsed.update(parsed_2)

        extracted_obj = LeadExtraction(**{k: parsed.get(k) for k in LeadExtraction.model_fields})

    except Exception as e:
        print(f"[Lead Extraction] LLM Failed: {e}")
        raise RuntimeError(f"AI Extraction failed due to API limits or model error: {e}")

    # --- Cost & Analytics ---
    tot_tok = in_tok + out_tok
    cost = 0.0
    model_name_used = getattr(llm, 'model_name', getattr(llm, 'model', os.getenv("PRIMARY_MODEL", "unknown")))

    # Basic cost calculation logic
    model_lower = model_name_used.lower()
    if "llama-3.3-70b" in model_lower:
        cost = (in_tok / 1_000_000 * 0.59) + (out_tok / 1_000_000 * 0.79)
    elif "llama-3.1-8b" in model_lower:
        cost = (in_tok / 1_000_000 * 0.05) + (out_tok / 1_000_000 * 0.08)
    elif "llama-3.2-90b" in model_lower:
        cost = (in_tok / 1_000_000 * 0.90) + (out_tok / 1_000_000 * 0.90)
    elif "gpt-4o-mini" in model_lower:
        cost = (in_tok / 1_000_000 * 0.150) + (out_tok / 1_000_000 * 0.600)
    elif "gpt-4o" in model_lower:
        cost = (in_tok / 1_000_000 * 2.50) + (out_tok / 1_000_000 * 10.00)

    execution_time_ms = int((time.time() - start_time) * 1000)

    # Extract confidence score from LLM, fallback to 0 (NOT 90) if it fails
    try:
        confidence_score_val = int(parsed.get("confidence_score", 0))
    except (TypeError, ValueError):
        confidence_score_val = 0
    confidence_level_val = str(parsed.get("confidence_level", "high")).lower()

    # 6. Save Telemetry — matches the REAL save_ai_email_parsing_async signature in .py
    from db.database import save_ai_email_parsing_async
    asyncio.create_task(save_ai_email_parsing_async(
        employee_id=employee_id,
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

    # 8. Client Type (fuzzy match via CRM API)
    company = extracted_dict.get('company_name') or ""
    contact = extracted_dict.get('contact_name') or sender_name or ""
    c_email  = extracted_dict.get('contact_email') or sender_email or ""

    client_type_result = await resolve_client_type(company, contact, c_email)

    # 9. Final Response (mirrors Lambda finalExtracted shape exactly)
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
        "lead_source":          lead_source,
        "debug_info":           client_type_result.get("debug")
    }
