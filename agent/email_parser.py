import re
import json
import os
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
    
    # Collapse excessive whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
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
- NEVER extract the email recipient (e.g. "Dear Mr. Arpit") or internal colleagues as a customer.

RULE 3 — contact_name and contact_phone:
- Extract contact_name if the email mentions a third-party client contact or prospect person by name (e.g., "the client contact is Mr. Usama", "contact: John", "reach out to Sarah").
- ALWAYS extract this even if you also used it for customer_name.
- If a phone number or any contact number is mentioned, extract it as contact_phone (e.g. "39579966").
- NEVER extract internal employees, colleagues, or the person the email is addressed to (e.g. "Dear Mr. Arpit") as the contact_name. Otherwise null.

RULE 4 — intent (choose EXACTLY one):
- "Estimation" = cost estimate request, technical estimation, or if subject mentions "Estimation Pending" (CRITICAL: Prioritize this over "Service Lead" even if "Sales Lead" is in the subject).
- "Service Lead" = new client pitched, new business opportunity (do not use if they are explicitly asking for an estimation or proposal).
- "General Task" = internal update, team task, meeting follow-up.
- "Proposal" = existing client wants a proposal.
- "Engagement Letter" = request or signoff for an engagement letter.
- "Invoice" = billing related.
- "Leave Request" = employee leave application (annual, sick, emergency, maternity, etc.). MUST be used whenever subject or body mentions leave, vacation, day off, absence.
- "HR Request" = other internal HR requests (salary inquiry, documents, onboarding, NOC letter, etc.).

RULE 5 — service_line_hint:
- Extract from context or sender signature. "BPS" = "Business Process Services".
- For Leave Request or HR Request, set null.

RULE 6 — task_description:
- Write a highly detailed, multi-sentence actionable summary of what needs to be done.
- Include ALL specific context, issues, or requests mentioned in the email body or subject line.
- If no bullet list exists, construct a clear 2-3 sentence description from the email body text.
- CRITICAL INSTRUCTION: If there is an "=== ATTACHMENT: ... ===" block, YOU MUST extract the actual data, names, numbers, or details from the attachment text and include them in this description. DO NOT just say "review the attached document". You must actually summarize what the document says!
- If images/screenshots are attached, explicitly describe what they show and include that in the context.
- For leave requests: "Process leave request from [sender name]. Review the leave dates and approve or reject via the HR system."
- NEVER output meta-commentary like "No description available". Always write something actionable.

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

Return ONLY valid JSON with these exact keys:
intent, task_title, project_name, customer_name, contact_name, contact_phone, service_line_hint, task_description, sender_name, sender_designation, due_date, priority, task_tag, invoice_amount, confidence_score, confidence_level

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

    # 2. Build Langchain payload
    from config.llm_factory import get_llm
    from langchain_core.messages import SystemMessage, HumanMessage
    
    user_content = [{"type": "text", "text": masked_prompt}]
    if image_contents:
        user_content.extend(image_contents)
        
    messages = [
        SystemMessage(content="You are a precise CRM extraction engine. Output ONLY valid JSON, no markdown, no explanation, no extra text."),
        HumanMessage(content=user_content if image_contents else masked_prompt)
    ]
    
    is_vision = bool(image_contents)
    try:
        llm = get_llm(temperature=0.0, is_vision=is_vision)
        
        # Try to enforce JSON mode if supported
        if not is_vision and hasattr(llm, "bind"):
            try:
                llm = llm.bind(response_format={"type": "json_object"})
            except Exception:
                pass
                
        # Print & log exact prompt sent to cloud AI for security verification
        print("\n" + "="*70)
        print("🔒 [SECURITY AUDIT] MASKED EMAIL TEXT SENT TO EXTERNAL CLOUD AI (Groq):")
        print("-" * 70)
        email_text_only = masked_prompt.split("Email Text:")[-1].strip() if "Email Text:" in masked_prompt else masked_prompt
        print(email_text_only)
        print("="*70 + "\n")

        response = llm.invoke(messages)
        
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
                model_name_used = os.getenv("VISION_MODEL", "llama-3.2-90b-vision-preview") if is_vision else os.getenv("PRIMARY_MODEL", "llama-3.3-70b-versatile")

            has_att = len(attachments) > 0 if attachments else False
            ext = None
            if has_att and isinstance(attachments, list):
                first_name = str(attachments[0].get("name", ""))
                if "." in first_name:
                    ext = "." + first_name.split(".")[-1].lower()
                    
            model_lower = str(model_name_used).lower()
            cost = 0.0
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
        
        content = response.content or "{}"
        
        # Clean markdown if model returned any
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
            
        parsed = json.loads(content.strip())
        
        # 0b. Local Unmasking: Restore real names, numbers, amounts in JSON result
        parsed = unmask_data(parsed, token_mapping)
        
        # Fallback regex extraction if LLM omitted contact_name / contact_phone
        if not parsed.get("contact_name"):
            name_m = re.search(r'(?:client contact|contact person|prospect contact|reach out to|contact)\s+(?:is\s+)?(Mr\.|Mrs\.|Ms\.|Dr\.)?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})', text, re.IGNORECASE)
            if name_m:
                parsed["contact_name"] = ((name_m.group(1) + " ") if name_m.group(1) else "") + name_m.group(2)

        if not parsed.get("contact_phone"):
            phone_m = re.search(r'(?:reached at|phone|mobile|tel|call|contact number)[:\s]+(?:\+?\d{1,4}[-\s]*)?(\d{7,14})', text, re.IGNORECASE)
            if phone_m:
                parsed["contact_phone"] = phone_m.group(1)

        # Fallback for new customers where LLM refuses to put a person's name as a company name
        if not parsed.get("customer_name") and parsed.get("contact_name"):
            parsed["customer_name"] = parsed["contact_name"]

        # 0c. Backend DB Verification — Cases 1 to 5
        try:
            parsed = enrich_email_with_db_cases(parsed)
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
            
            emp_id = employee_id if (employee_id and employee_id != 0) else None
            clean_ref_id = str(reference_id) if (reference_id and str(reference_id).strip() and str(reference_id).strip().lower() != "none") else f"draft_{int(time.time() * 1000)}"
            ref_str = clean_ref_id[:255]

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
                processing_status="SUCCESS",
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
                confidence_score=conf_score_val
            ))
        except Exception as dbe:
            print(f"Direct email_task & ML telemetry logging error: {dbe}")
            
        if isinstance(parsed, dict):
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


def enrich_email_with_db_cases(parsed: dict) -> dict:
    """
    Backend MySQL verification for the 5 Email-to-Task business cases.
    Uses ONLY verified real table names: customers, projects, m_serviceline, proposal, invoice.

    Case 1: Registered customer found -> set db_customer_id, db_customer_registered
    Case 2: Existing customer, new service line -> set intent=Service Lead, new_service_line=True
    Case 3: Existing customer, existing service line -> keep intent=General Task
    Case 4: Proposal/Engagement Letter request -> fetch latest proposal from `proposal` table
    Case 5: Invoice intent -> fetch latest invoice from `invoice` table
    """
    if not isinstance(parsed, dict):
        return parsed

    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()

        cust_name = (parsed.get("customer_name") or "").strip()
        intent_raw = str(parsed.get("intent") or "").lower()
        desc_raw = str(parsed.get("task_description") or "").lower()

        with engine.connect() as conn:
            # ── Step 1: Look up customer in `customers` table ──────────────────
            customer_id = None
            if cust_name and len(cust_name) > 2:
                c_row = conn.execute(
                    text("SELECT id FROM customers WHERE LOWER(customer_name) LIKE :name AND is_active = 1 LIMIT 1"),
                    {"name": f"%{cust_name.lower()}%"}
                ).fetchone()
                if c_row:
                    customer_id = c_row[0]
                    parsed["db_customer_id"] = customer_id
                    parsed["db_customer_registered"] = True
                    print(f"[enrich_db_cases] Case 1: Customer found in DB — id={customer_id}")

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
                            "SELECT id, invoice_no, total_amt_ex_vat, created_at FROM invoice "
                            "WHERE client_id = :cid AND is_active = 1 "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"cid": customer_id}
                    ).fetchone()
                    if inv_row:
                        parsed["latest_invoice_number"] = inv_row[1]
                        parsed["invoice_amount"] = str(inv_row[2])
                        parsed["latest_invoice_date"] = str(inv_row[3])
                        print(f"[enrich_db_cases] Case 5: Latest invoice no={inv_row[1]}, date={inv_row[3]}")

            # ── Cases 2 & 3: Existing Customer — Service Line Check ────────────
            elif customer_id:
                sl_hint = (parsed.get("service_line_hint") or "").strip().lower()

                # Fetch all distinct service lines this customer has been served under
                sl_rows = conn.execute(
                    text(
                        "SELECT DISTINCT p.service_line_id FROM projects p "
                        "WHERE p.client = :cid AND p.is_active = 1"
                    ),
                    {"cid": customer_id}
                ).fetchall()
                existing_sl_ids = {r[0] for r in sl_rows if r[0]}

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
                            # Case 3: Existing service line -> follow-up
                            parsed["db_case"] = 3
                            print(f"[enrich_db_cases] Case 3: Existing service line match sl_id={hinted_sl_id}")
                    else:
                        # Service line hint not found in master table
                        parsed["db_case"] = 1
                else:
                    # No service line hint — treat as Case 1 (registered customer general task)
                    parsed["db_case"] = 1
                    print(f"[enrich_db_cases] Case 1: Registered customer, no SL hint")

    except Exception as dbe:
        print(f"[enrich_email_with_db_cases] DB Verification Error: {dbe}")

    return parsed


def build_general_query_mapping(parsed: dict) -> dict:
    """
    Builds pre-filled UI chip parameters and redirection metadata
    matching MGeneralRequestType, MSubject, MGeneralIssue, and Project schema.
    """
    intent = str(parsed.get("intent") or "").strip().lower()
    task_description = str(parsed.get("task_description") or "").strip()
    priority = str(parsed.get("priority") or "Medium").strip().capitalize()
    project_name = parsed.get("project_name")
    
    # 1. Determine Request Type, Subject, and Query based on intent & description
    req_type_name = "CRM Issues"
    subject_name = "General Query"
    query = "Others"
    
    if "leave" in intent or "vacation" in intent or "day off" in intent or "leave" in task_description.lower():
        req_type_name = "HR"
        subject_name = "Leave Request"
        query = "Leave Request"
    elif "hr" in intent or "payslip" in task_description.lower() or "salary" in task_description.lower():
        req_type_name = "HR"
        subject_name = "HR Query"
        query = "HR Query"
    elif "invoice" in intent or "billing" in intent or "payable" in task_description.lower() or "payment" in task_description.lower():
        req_type_name = "Finance"
        if "receivable" in task_description.lower() or "collection" in task_description.lower():
            subject_name = "Receivables Management"
            query = "Others"
        else:
            subject_name = "Payable Management"
            if "reimbursement" in task_description.lower():
                query = "Reimbursement"
            elif "tender" in task_description.lower():
                query = "Tender Bond"
            elif "confirmation" in task_description.lower():
                query = "Payment Confirmation Copy"
            else:
                query = "Others"
    elif "it" in intent or "password" in task_description.lower() or "access" in task_description.lower() or "software" in task_description.lower():
        req_type_name = "IT Support"
        subject_name = "General Query"
        query = "Others"
    elif "service lead" in intent or "lead" in intent:
        req_type_name = "Client Support"
        subject_name = "General Query"
        query = "Others"
    elif "proposal" in intent or "estimation" in intent:
        req_type_name = "Client Support"
        subject_name = "Proposal" if "proposal" in intent else "Job Estimation"
        query = "Others"
    elif "client" in intent or "customer" in task_description.lower():
        req_type_name = "Client Support"
        subject_name = "General Query"
        query = "Others"
    else:
        req_type_name = "Client Support"
        if "export" in task_description.lower() or "excel" in task_description.lower() or "report" in task_description.lower():
            subject_name = "Reports"
            query = "Not able to export to excel"
        else:
            subject_name = "General Query"
            query = "Others"

    # Evaluate number of non-default extracted fields
    populated_fields = 0
    if req_type_name and req_type_name != "CRM Issues":
        populated_fields += 1
    if subject_name and subject_name != "General Query":
        populated_fields += 1
    if query and query != "Others":
        populated_fields += 1
    if project_name:
        populated_fields += 1
    if parsed.get("customer_name"):
        populated_fields += 1
    if task_description and len(task_description) > 10:
        populated_fields += 1

    insufficient_info = populated_fields < 3

    mapping = {
        "req_type_name": req_type_name,
        "subject_name": subject_name,
        "query": query,
        "priority": priority if priority in ["Low", "Medium", "High"] else "Medium",
        "sub_detail": task_description,
        "project_name": project_name,
        "customer_name": parsed.get("customer_name"),
        "field_count": populated_fields,
        "insufficient_info": insufficient_info,
        "insufficient_message": "Notice: Limited details were extracted from this email (fewer than 3 fields matched). Please verify and complete the query form manually." if insufficient_info else None
    }
    
    redirection_prompt = {
        "message": "Notice: Fewer than 3 fields could be extracted by AI. Do you want to redirect to General Query to complete the missing details manually?" if insufficient_info else "Do you want to redirect to General Query for this task?",
        "options": {
            "yes": {
                "label": "Yes",
                "redirect_to": "/self-services/general-queries",
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


