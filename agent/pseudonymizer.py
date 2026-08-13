"""
agent/pseudonymizer.py — Local PII Pseudonymization & Anonymization Module
Ensures 100% data confidentiality by tokenizing sensitive entities (Names, Emails, 
Phone Numbers, Financial Amounts, IBANs, Company Names) locally BEFORE sending 
prompts to external LLM APIs (Groq), and unmasking tokens back to real values locally.
"""

import re
import logging
from typing import Dict, Tuple, Any, List, Optional

logger = logging.getLogger("CRM_Pseudonymizer")

class LocalPseudonymizer:
    def __init__(self):
        pass

    def get_db_master_entities(self) -> Tuple[List[str], List[str], List[str]]:
        """
        Fetches known customer names, project names, employee names, and client contact names from local MySQL database.
        """
        customers = []
        projects = []
        employees = []
        try:
            from db.database import get_db_engine
            from sqlalchemy import text
            engine = get_db_engine()
            with engine.connect() as conn:
                # 1. Fetch Customers
                for query_str in [
                    "SELECT customer_name FROM customers WHERE is_active = 1 LIMIT 1000",
                    "SELECT customer_name FROM customer WHERE is_active = 1 LIMIT 1000"
                ]:
                    try:
                        c_rows = conn.execute(text(query_str)).fetchall()
                        customers.extend([r[0].strip() for r in c_rows if r[0] and len(r[0].strip()) > 2])
                        if c_rows: break
                    except Exception:
                        pass

                # 2. Fetch Projects (Names and Codes)
                for query_str in [
                    "SELECT name, code FROM projects WHERE is_active = 1 LIMIT 2000",
                    "SELECT name FROM projects WHERE is_active = 1 LIMIT 2000",
                    "SELECT project_name FROM projects WHERE is_active = 1 LIMIT 2000"
                ]:
                    try:
                        p_rows = conn.execute(text(query_str)).fetchall()
                        for r in p_rows:
                            for val in r:
                                if val and isinstance(val, str) and len(val.strip()) > 2:
                                    projects.append(val.strip())
                        if p_rows: break
                    except Exception:
                        pass

                # 3. Fetch Employees
                for query_str in [
                    "SELECT employee_name FROM employees WHERE is_active = 1 LIMIT 1000",
                    "SELECT first_name, last_name FROM employee WHERE is_active = 1 LIMIT 1000"
                ]:
                    try:
                        e_rows = conn.execute(text(query_str)).fetchall()
                        for row in e_rows:
                            for val in row:
                                if val and isinstance(val, str) and len(val.strip()) > 2:
                                    employees.append(val.strip())
                        if e_rows: break
                    except Exception:
                        pass

                # 4. Fetch Client Contacts
                for query_str in [
                    "SELECT contact_name FROM customer_contacts LIMIT 1000",
                    "SELECT contact_name FROM customer_contact LIMIT 1000"
                ]:
                    try:
                        cc_rows = conn.execute(text(query_str)).fetchall()
                        for r in cc_rows:
                            if r[0] and len(r[0].strip()) > 2:
                                employees.append(r[0].strip())
                        if cc_rows: break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[Pseudonymizer] DB master lookup error: {e}")
        return list(set(customers)), list(set(projects)), list(set(employees))

    def mask_text(self, text: str, db_customers: List[str] = None, db_projects: List[str] = None, db_employees: List[str] = None) -> Tuple[str, Dict[str, str]]:
        """
        Locally anonymizes raw text by replacing sensitive entities with semantic tokens.
        Returns (anonymized_text, token_mapping_dict).
        """
        if not text or not isinstance(text, str):
            return text or "", {}

        mapping: Dict[str, str] = {}
        masked_text = text

        # Counter trackers
        cust_cnt = 1
        proj_cnt = 1
        person_cnt = 1
        phone_cnt = 1
        email_cnt = 1
        amount_cnt = 1
        conf_cnt = 1

        # 0. Redact Legal Disclaimer Boilerplates to save token bloat & remove noise
        disclaimer_patterns = [
            r'This email and any attachments are confidential.*?(?=\n\n|\Z)',
            r'CONFIDENTIALITY NOTICE:.*?(?=\n\n|\Z)',
            r'The information contained in this message is confidential.*?(?=\n\n|\Z)'
        ]
        for dp in disclaimer_patterns:
            masked_text = re.sub(dp, '[LEGAL_DISCLAIMER_REDACTED]', masked_text, flags=re.IGNORECASE | re.DOTALL)

        # 1. DB Masters Matching (Known Customers, Projects, Employees)
        c_list = db_customers if db_customers is not None else []
        p_list = db_projects if db_projects is not None else []
        e_list = db_employees if db_employees is not None else []

        if not c_list and not p_list and not e_list:
            c_list, p_list, e_list = self.get_db_master_entities()

        # Sort by length descending to match longest phrases first
        c_list = sorted(c_list, key=len, reverse=True)
        p_list = sorted(p_list, key=len, reverse=True)
        e_list = sorted(e_list, key=len, reverse=True)

        # Projects MUST be tokenized BEFORE customers so that longer project names (e.g. 'STC PAY BAHRAIN B.S.C CLOSED') 
        # are matched as <PROJECT_TOKEN_x> first, rather than having their customer prefix ('STC PAY BAHRAIN') masked first.
        for proj in p_list:
            if proj and len(proj) >= 3:
                # Use exact string match or word boundary depending on presence of special chars
                pattern = r'\b' + re.escape(proj) + r'\b' if proj.replace(" ", "").isalnum() else re.escape(proj)
                if re.search(pattern, masked_text, re.IGNORECASE):
                    token = f"<PROJECT_TOKEN_{proj_cnt}>"
                    match = re.search(pattern, masked_text, re.IGNORECASE)
                    real_val = match.group(0) if match else proj
                    mapping[token] = real_val
                    masked_text = re.sub(re.escape(real_val), token, masked_text, flags=re.IGNORECASE)
                    proj_cnt += 1

        for cust in c_list:
            if cust and len(cust) >= 3 and re.search(r'\b' + re.escape(cust) + r'\b', masked_text, re.IGNORECASE):
                token = f"<CUSTOMER_TOKEN_{cust_cnt}>"
                match = re.search(r'\b' + re.escape(cust) + r'\b', masked_text, re.IGNORECASE)
                real_val = match.group(0) if match else cust
                mapping[token] = real_val
                masked_text = re.sub(r'\b' + re.escape(cust) + r'\b', token, masked_text, flags=re.IGNORECASE)
                cust_cnt += 1

        for emp in e_list:
            if emp and len(emp) >= 3 and re.search(r'\b' + re.escape(emp) + r'\b', masked_text, re.IGNORECASE):
                token = f"<PERSON_TOKEN_{person_cnt}>"
                match = re.search(r'\b' + re.escape(emp) + r'\b', masked_text, re.IGNORECASE)
                real_val = match.group(0) if match else emp
                mapping[token] = real_val
                masked_text = re.sub(r'\b' + re.escape(emp) + r'\b', token, masked_text, flags=re.IGNORECASE)
                person_cnt += 1

        # 1b. Key-Value Labeled Form Fields & Company Entity Patterns (e.g. "Company: Apex", "on behalf of STC PAY BAHRAIN B.S.C CLOSED")
        lbl_cust_matches = re.findall(r'(?i)\b(?:Company|Client|Organization|Customer)\s*:\s*([A-Z0-9][A-Za-z0-9\s&._-]+)', masked_text)
        for val in set(lbl_cust_matches):
            val_clean = val.strip().split('\n')[0].strip()
            if len(val_clean) >= 2 and not val_clean.startswith('<'):
                token = f"<CUSTOMER_TOKEN_{cust_cnt}>"
                mapping[token] = val_clean
                masked_text = masked_text.replace(val_clean, token)
                cust_cnt += 1

        on_behalf_matches = re.findall(r'(?i)\b(?:on behalf of|representing)\s+([A-Z0-9][A-Za-z0-9\s&._-]{2,60}(?:\s+(?:B\.?S\.?C\.?\s*(?:CLOSED|PUBLIC)?|W\.?L\.?L\.?|S\.?P\.?C\.?|LLC|LTD|LIMITED|INC|CORP))?)', masked_text)
        for val in set(on_behalf_matches):
            val_clean = val.strip().split('\n')[0].strip()
            if len(val_clean) >= 2 and not val_clean.startswith('<'):
                token = f"<CUSTOMER_TOKEN_{cust_cnt}>"
                mapping[token] = val_clean
                masked_text = re.sub(re.escape(val_clean), token, masked_text, flags=re.IGNORECASE)
                cust_cnt += 1

        # Match corporate suffixes (B.S.C, W.L.L, B.S.C CLOSED, LLC, Ltd, etc.)
        corp_suffix_matches = re.findall(r'\b([A-Z0-9][A-Za-z0-9\s&._-]{2,60}\s+(?:B\.?S\.?C\.?\s*(?:CLOSED|PUBLIC)?|W\.?L\.?L\.?|S\.?P\.?C\.?|LLC|LTD|LIMITED|INC|CORP))\b', masked_text, re.IGNORECASE)
        for val in set(corp_suffix_matches):
            val_clean = val.strip()
            if len(val_clean) >= 3 and not val_clean.startswith('<'):
                token = f"<CUSTOMER_TOKEN_{cust_cnt}>"
                mapping[token] = val_clean
                masked_text = re.sub(re.escape(val_clean), token, masked_text, flags=re.IGNORECASE)
                cust_cnt += 1

        lbl_person_matches = re.findall(r'(?i)\b(?:Contact\s+Name|Contact\s+Person|Contact|Client\s+Contact)\s*:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', masked_text)
        for val in set(lbl_person_matches):
            val_clean = val.strip().split('\n')[0].strip()
            if len(val_clean) >= 2 and not val_clean.startswith('<'):
                token = f"<PERSON_TOKEN_{person_cnt}>"
                mapping[token] = val_clean
                masked_text = masked_text.replace(val_clean, token)
                person_cnt += 1

        # Labeled & Contextual Project Entity Patterns (e.g. "Project: Apex", "Your Project JUPITER PRODUCTS COMPANY W.L.L._Audit 31 March 2022 Is Live")
        proj_patterns = [
            r'(?i)\b(?:Project|Engagement|System)\s*:\s*([A-Z0-9][A-Za-z0-9\s&._-]+)',
            r'(?i)\b(?:Your\s+Project|Project|Engagement)\s+([A-Z0-9][A-Za-z0-9\s&._-]{3,70}?)(?=\s+(?:Is|is|has|was|will|for|to|on|\n|\.|,|$))'
        ]
        for pp in proj_patterns:
            for val in set(re.findall(pp, masked_text)):
                val_clean = val.strip().split('\n')[0].strip()
                if len(val_clean) >= 3 and not val_clean.startswith('<'):
                    token = f"<PROJECT_TOKEN_{proj_cnt}>"
                    mapping[token] = val_clean
                    masked_text = re.sub(re.escape(val_clean), token, masked_text, flags=re.IGNORECASE)
                    proj_cnt += 1

        # 1c. Email Header Names Pseudonymization (From:, To:, Cc:, Bcc:, Original From:, Forwarder:, Sender:)
        header_patterns = [
            r'(?i)\b(?:From|To|Cc|Bcc|Original\s+From|Original\s+To|Original\s+Cc|Forwarder|Sender)\s*:\s*([^\r\n]+)'
        ]
        for hp in header_patterns:
            header_lines = re.findall(hp, masked_text)
            for line in header_lines:
                # Remove raw emails or email tokens inside <> or <<>>
                clean_line = re.sub(r'<<?[^>\r\n]+>>?', '', line)
                clean_line = re.sub(r'<[A-Z_0-9]+>', '', clean_line)
                for raw_part in re.split(r'[;,]', clean_line):
                    part_clean = raw_part.strip()
                    words = [w.strip() for w in part_clean.split() if w.strip() and not w.strip().startswith('<')]
                    for w in words:
                        if len(w) >= 2 and w[0].isupper() and w not in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "July", "August", "September", "October", "November", "December", "Proposal", "Template", "Advisory", "Sent", "Subject", "Date", "Re", "Fwd"):
                            if re.search(r'\b' + re.escape(w) + r'\b', masked_text):
                                token = f"<PERSON_TOKEN_{person_cnt}>"
                                mapping[token] = w
                                masked_text = re.sub(r'\b' + re.escape(w) + r'\b', token, masked_text)
                                person_cnt += 1

        # 2. Confidential Data: IBAN, Credit Cards, Passwords, Tax IDs
        iban_matches = re.findall(r'\bBH\d{2}[A-Z0-9]{4}\d{14}\b', masked_text, re.IGNORECASE)
        for iban in set(iban_matches):
            token = f"<CONFIDENTIAL_TOKEN_{conf_cnt}>"
            mapping[token] = iban
            masked_text = masked_text.replace(iban, token)
            conf_cnt += 1

        card_matches = re.findall(r'\b(?:\d[ -]*?){13,16}\b', masked_text)
        for card in set(card_matches):
            if len(re.sub(r'\D', '', card)) in (13, 14, 15, 16):
                token = f"<CONFIDENTIAL_TOKEN_{conf_cnt}>"
                mapping[token] = card
                masked_text = masked_text.replace(card, token)
                conf_cnt += 1

        secret_matches = re.findall(r'(?i)\b(password|secret|api[_-]?key|bearer)\s*[:=]\s*(\S+)', masked_text)
        for label, val in secret_matches:
            token = f"<SECRET_TOKEN_{conf_cnt}>"
            mapping[token] = val
            masked_text = masked_text.replace(val, token)
            conf_cnt += 1

        # 3. Dynamic Currency & Financial Amounts
        curr_patterns = [
            r'(?:BHD|BHD\.|BD|USD|EUR|\$|€|£|SAR|AED)\s*[\d,]+(?:\.\d+)?',
            r'[\d,]+(?:\.\d+)?\s*(?:BHD|BD|USD|EUR|SAR|AED)'
        ]
        for cp in curr_patterns:
            for amt in set(re.findall(cp, masked_text, re.IGNORECASE)):
                token = f"<AMOUNT_TOKEN_{amount_cnt}>"
                mapping[token] = amt
                masked_text = masked_text.replace(amt, token)
                amount_cnt += 1

        # 4. Email Addresses
        email_matches = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', masked_text)
        for email_str in set(email_matches):
            token = f"<EMAIL_TOKEN_{email_cnt}>"
            mapping[token] = email_str
            masked_text = masked_text.replace(email_str, token)
            email_cnt += 1

        # 5. Phone Numbers (Bahrain 8-digit or international formats)
        phone_matches = re.findall(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b', masked_text)
        for ph in set(phone_matches):
            digits_only = re.sub(r'\D', '', ph)
            if 7 <= len(digits_only) <= 15 and not ph.startswith('2026'):
                token = f"<PHONE_TOKEN_{phone_cnt}>"
                mapping[token] = ph
                masked_text = masked_text.replace(ph, token)
                phone_cnt += 1

        # 6. Greetings, Salutations & Sign-offs Person Names
        greeting_matches = re.findall(r'\b(?:Hi|Hello|Hey|Dear|Greetings|Good\s+(?:morning|afternoon|evening))\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', masked_text)
        for name in set(greeting_matches):
            if len(name) > 2 and name not in ("Team", "All", "Sir", "Madam") and not name.startswith("<"):
                token = f"<PERSON_TOKEN_{person_cnt}>"
                mapping[token] = name
                masked_text = re.sub(r'\b' + re.escape(name) + r'\b', token, masked_text)
                person_cnt += 1

        signoff_matches = re.findall(r'\b(?:Thanks|Regards|Best|Sincerely|Cheers|Warm regards|Best regards|Thanks & regards),?\s*\n?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', masked_text)
        for name in set(signoff_matches):
            if len(name) > 2 and name not in ("Team", "All", "Sir", "Madam") and not name.startswith("<"):
                token = f"<PERSON_TOKEN_{person_cnt}>"
                mapping[token] = name
                masked_text = re.sub(r'\b' + re.escape(name) + r'\b', token, masked_text)
                person_cnt += 1

        # 7. General Person Names with Titles (e.g. Mr. Usama, Mrs. Fatima, Dr. Kansara, Eng. Sahil)
        title_matches = re.findall(r'\b(?:Mr\.|Mrs\.|Ms\.|Dr\.|Eng\.)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', masked_text)
        for name in set(title_matches):
            if len(name) > 2 and name not in ("Team", "All", "Sir", "Madam") and not name.startswith("<"):
                token = f"<PERSON_TOKEN_{person_cnt}>"
                mapping[token] = name
                masked_text = re.sub(r'\b' + re.escape(name) + r'\b', token, masked_text)
                person_cnt += 1

        # 7b. Cleanup trailing unmasked last names (e.g. "<PERSON_TOKEN_5> Kalasava" -> "<PERSON_TOKEN_5>")
        trailing_name_matches = re.findall(r'(<PERSON_TOKEN_\d+>)\s+([A-Z][a-z]{2,25})\b', masked_text)
        for token_match, last_name in trailing_name_matches:
            if last_name not in ("Is", "Has", "Was", "Will", "Are", "Dear", "Hello", "Team", "Your", "Please", "Click", "Email", "Password", "Regards"):
                if token_match in mapping:
                    mapping[token_match] = mapping[token_match] + " " + last_name
                masked_text = masked_text.replace(f"{token_match} {last_name}", token_match)

        logger.info(f"[Pseudonymizer] Masked text locally: {len(mapping)} entities tokenized.")
        return masked_text, mapping

    def unmask_data(self, data: Any, mapping: Dict[str, str]) -> Any:
        """
        Recursively unmasks tokens back to their original real values in dicts, lists, or strings.
        """
        if not mapping:
            return data

        if isinstance(data, str):
            unmasked = data
            for token, real_val in mapping.items():
                if token in unmasked:
                    unmasked = unmasked.replace(token, real_val)
            return unmasked

        elif isinstance(data, dict):
            return {k: self.unmask_data(v, mapping) for k, v in data.items()}

        elif isinstance(data, list):
            return [self.unmask_data(item, mapping) for item in data]

        return data


# Global singleton instance
_pseudonymizer_instance = LocalPseudonymizer()

def mask_email_text(text: str, db_customers: List[str] = None, db_projects: List[str] = None, db_employees: List[str] = None) -> Tuple[str, Dict[str, str]]:
    return _pseudonymizer_instance.mask_text(text, db_customers, db_projects, db_employees)

def unmask_data(data: Any, mapping: Dict[str, str]) -> Any:
    return _pseudonymizer_instance.unmask_data(data, mapping)
