import re
import json
import os
import fitz  # PyMuPDF
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

def extract_entities_with_llm(text: str, sender_type: str, is_forwarded: bool, attachments: list = None, employee_id: int = 0, reference_id: str = None) -> dict:
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
- Extract the full project name if mentioned. Project names can be long strings (e.g. "Company XYZ_Audit for 2021 and 2022") or short codes (e.g. "BT-PR-065667").
- Short abbreviations like "BPS", "VAT", "MIS", "CBB" are services, NOT project names. Set null.
- CRITICAL: If the intent is "General Task" or an internal task, you MAY extract the project_name ONLY IF it is an explicitly mentioned, existing active project. DO NOT extract prospective ideas, future tools, or general system descriptions (e.g. "AI-Powered System") as a project_name.
- ONLY if the intent is "Service Lead" or "Proposal" should you extract a proposed scope/idea as the project_name.
- For Leave Request, HR Request, or Internal Support emails, always set null.

RULE 2 — customer_name:
- You MUST extract the COMPANY or ORGANIZATION name if present.
- CRITICAL: If no company name is found, but a client/prospect person's name is mentioned, YOU MUST extract that person's name as the customer_name (e.g., "Mr. Usama"). Do not leave this null if a person is mentioned as the client!
- For Leave Request, HR Request, or Internal Support emails, always set null.
- NEVER extract the email recipient (e.g. "Dear Mr. Arpit") or the sender as a customer.

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
- "Internal Support" = internal IT support, CRM issues, system access, or administrative requests not related to any client.

RULE 5 — service_line_hint:
- Extract from context or sender signature. "BPS" = "Business Process Services".
- CRITICAL: If the intent is "Service Lead", you MUST try to infer the relevant service line (e.g., "Technology", "Audit", "Tax", "Advisory", "BPS") based on the services being requested or proposed.
- For Leave Request, HR Request, or Internal Support, set null.

RULE 6 — task_description:
- Write a highly detailed, multi-sentence actionable summary of what needs to be done.
- Include ALL specific context, issues, or requests mentioned in the email (e.g., specific buttons missing, exact error messages, specific terms).
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

RULE 12 — EXTRACTION CONFIDENCE:
- Evaluate your own extraction confidence based on: email clarity, sender identified, tasks clearly identified, due date/priority confidently extracted, customer/project identified, attachments understood, and overall completeness.
- Calculate a precise `confidence_score` between 0 and 100. START AT 100, then DEDUCT points:
   * Deduct 7 points if priority is not explicitly mentioned (inferred)
   * Deduct 13 points if due date is missing
   * Deduct 18 points if customer/project name is missing or ambiguous
   * Deduct 5 points if service line is missing or inferred
   * Deduct 11 points if no clear action items are found
- The final score MUST reflect the exact math of these deductions (e.g., 88, 79, 93). DO NOT just output 90.
- Generate a `confidence_level`: "High" (80-100), "Medium" (50-79), or "Low" (0-49).
- Generate a `confidence_reason` array of strings explaining the confidence. Prefix each reason with "✓" if it was successful or "⚠" if there was an issue or ambiguity (e.g., ["✓ Task clearly identified", "⚠ Priority inferred from context"]).

Return ONLY valid JSON with these exact keys:
intent, project_name, customer_name, contact_name, contact_phone, service_line_hint, task_description, sender_name, sender_designation, due_date, priority, task_tag, invoice_amount, confidence_score, confidence_level, confidence_reason

Email Text:
{text}"""

    attachments = attachments or []
    pdf_text_blocks = []
    image_contents = []
    parsed_attachments_count = 0

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
                parsed_attachments_count += 1
        elif "wordprocessingml" in ct or att_name.endswith(".docx"):
            docx_text = extract_text_from_docx_base64(content_bytes)
            if docx_text:
                pdf_text_blocks.append(f"=== ATTACHMENT: {att.get('name', 'DOCX')} ===\n{docx_text}")
                parsed_attachments_count += 1
        elif "image" in ct:
            image_contents.append({
                "type": "image_url",
                "image_url": {"url": f"data:{ct};base64,{content_bytes}"}
            })
            parsed_attachments_count += 1

    if pdf_text_blocks:
        prompt += "\n\n" + "\n\n".join(pdf_text_blocks)

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
    
    user_content = [{"type": "text", "text": prompt}]
    if image_contents:
        user_content.extend(image_contents)
        
    messages = [
        SystemMessage(content="You are a precise CRM extraction engine. Output ONLY valid JSON, no markdown, no explanation, no extra text."),
        HumanMessage(content=user_content if image_contents else prompt)
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
                
        response = llm.invoke(messages)
        
        # --- Token Tracking ---
        try:
            from db.database import save_parsing_token_usage
            in_tok = 0
            out_tok = 0
            tot_tok = 0
            
            if hasattr(response, 'response_metadata') and response.response_metadata:
                usage = response.response_metadata.get("token_usage", {})
                if not usage and "usage" in response.response_metadata:
                    # Some providers nest differently
                    usage = response.response_metadata["usage"]
                    
                in_tok = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                tot_tok = usage.get("total_tokens", 0)
                
            model_name_used = getattr(llm, 'model_name', getattr(llm, 'model', 'unknown'))
            
            has_att = len(attachments) > 0 if attachments else False
            ext = None
            if has_att and isinstance(attachments, list):
                first_name = str(attachments[0].get("name", ""))
                if "." in first_name:
                    ext = "." + first_name.split(".")[-1].lower()
                    
            # Basic cost calculation logic
            model_lower = model_name_used.lower()
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
                    
            token_tracking = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": tot_tok,
                "total_cost_usd": cost,
                "has_attachment": has_att,
                "file_extension": ext
            }
        except Exception as e:
            print(f"Error tracking token usage: {e}")
        # ----------------------
        
        content = response.content or "{}"
        
        # Clean markdown if model returned any
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
            
        parsed = json.loads(content.strip())
        
        # Fallback for new customers where LLM refuses to put a person's name as a company name
        if not parsed.get("customer_name") and parsed.get("contact_name"):
            parsed["customer_name"] = parsed["contact_name"]
            
        # Append parsing stats for backend
        parsed["_meta"] = {
            "model_name": model_name_used,
            "total_attachments": len(attachments),
            "parsed_attachments": parsed_attachments_count,
            "token_tracking": token_tracking
        }
            
        return parsed
    except Exception as e:
        print(f"Extraction error: {e}")
        return {}
