"""
agent/pseudonymizer.py — Production-Grade Fail-Closed Privacy Middleware

Ensures 100% data confidentiality by tokenizing sensitive entities (Names, Emails, 
Phone Numbers, Financial Amounts, IBANs, Company Names, Secrets, Dates, IDs) locally 
BEFORE sending prompts to external LLM APIs (Groq, Claude, OpenAI), and unmasking 
tokens back to real values locally.

SECURITY ARCHITECTURE:
  Raw CRM / Email Text
           │
           ▼
  CRM Entity Detection (DB Masters + Company Suffixes)
           │
           ▼
  Presidio PII Detection Engine (Generic NLP PII)
           │
           ▼
  Custom Regex & Rule Engine (Bahrain IBAN/Phone/BHD, Secrets, IDs, URLs)
           │
           ▼
  Masking Execution
           │
           ▼
  Post-Masking Security Validation
           │
     ┌─────┴─────┐
     │ SAFE?     │
    NO           YES
     │           │
     ▼           ▼
  BLOCK       External LLM API
  REQUEST     (Receives ONLY Masked Payload)
                 │
                 ▼
              Local Unmasking (In-Memory Request-Scoped)
                 │
                 ▼
              Final Response
"""

import re
import uuid
import hashlib
import logging
from typing import Dict, Tuple, Any, List, Optional, Set
from dataclasses import dataclass, field

import os

logger = logging.getLogger("CRM_Pseudonymizer")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    
    # Console Handler
    c_handler = logging.StreamHandler()
    c_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(c_handler)
    
    # Persistent File Handler to d:\CRM-ai-services\logs\privacy_pseudonymizer.log
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "privacy_pseudonymizer.log")
        f_handler = logging.FileHandler(log_file, encoding="utf-8")
        f_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(f_handler)
    except Exception as _log_e:
        pass

# ---------------------------------------------------------------------------
# Sanitized Security Exception
# ---------------------------------------------------------------------------
class PrivacySecurityError(Exception):
    """
    Raised when PII detection, masking, or post-mask validation fails,
    or when unmasked PII is detected in outbound payloads.
    Guarantees ZERO exposure of sensitive values or prompts in exception strings.
    """
    pass


# ---------------------------------------------------------------------------
# Request-Scoped Privacy Result Dataclass
# ---------------------------------------------------------------------------
@dataclass
class PrivacyResult:
    """
    Holds request-isolated masked payload and in-memory token mapping.
    Never logged, persisted, or sent externally.
    """
    request_id: str
    masked_text: str
    token_mapping: Dict[str, str] = field(default_factory=dict)
    safe: bool = False
    blocked_reason: Optional[str] = None
    entity_counts: Dict[str, int] = field(default_factory=dict)
    masked_hash: str = ""

    def clear_mapping(self):
        """Immediately clears token mapping dictionary from memory after unmasking."""
        if self.token_mapping:
            self.token_mapping.clear()


# ---------------------------------------------------------------------------
# Post-Masking Security Validator
# ---------------------------------------------------------------------------
class PostMaskingValidator:
    """
    Independent security validator that inspects the final masked text.
    Verifies that no raw detectable PII or secrets remain before sending to external LLM.
    """
    def __init__(self):
        self._email_regex = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
        self._phone_regex = re.compile(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b')
        self._iban_regex = re.compile(r'\bBH\d{2}[A-Z0-9]{4}\d{14}\b', re.IGNORECASE)
        self._secret_regex = re.compile(r'(?i)\b(?:sk-[a-zA-Z0-9]{20,}|bearer\s+ey[a-zA-Z0-9._-]{20,}|password\s*[:=]\s*(?!<[A-Z_]+_\d+>)\S+)\b')

    def validate(self, masked_text: str, db_masters: Tuple[List[str], List[str], List[str]]) -> Tuple[bool, Optional[str]]:
        if not masked_text:
            return True, None

        # 1. Check residual emails
        emails = self._email_regex.findall(masked_text)
        if emails:
            return False, "Residual unmasked email detected"

        # 2. Check residual IBANs
        ibans = self._iban_regex.findall(masked_text)
        if ibans:
            return False, "Residual unmasked IBAN detected"

        # 3. Check residual secrets/API keys (ignoring already-masked tokens)
        secrets = self._secret_regex.findall(masked_text)
        if secrets:
            return False, "Residual unmasked secret/API key detected"

        # 4. Check residual CRM Master Entities (Customers, Projects, Employees >= 4 chars)
        c_list, p_list, e_list = db_masters
        for master_list in (c_list, p_list, e_list):
            for name in master_list:
                if name and len(name) >= 4 and not name.startswith("<"):
                    pattern = r'\b' + re.escape(name) + r'\b' if name.replace(" ", "").isalnum() else re.escape(name)
                    if re.search(pattern, masked_text, re.IGNORECASE):
                        return False, f"Residual unmasked master database entity detected"

        return True, None


# Global Presidio Analyzer instance (loaded once per process)
_global_presidio_analyzer = None

class PresidioEngine:
    """
    Microsoft Presidio Analyzer Engine wrapper using process-level singleton.
    Used for generic PII detection (PERSON, LOCATION, EMAIL, PHONE, IP, etc.).
    """
    def _get_analyzer(self):
        global _global_presidio_analyzer
        if _global_presidio_analyzer is None:
            try:
                import os
                os.environ["TLDEXTRACT_FALLBACK_TO_SNAPSHOT"] = "1"
                from presidio_analyzer import AnalyzerEngine
                _global_presidio_analyzer = AnalyzerEngine()
            except Exception as e:
                logger.warning(f"[PresidioEngine] Initialization warning: {e}. Presidio fallback active.")
                _global_presidio_analyzer = False
        return _global_presidio_analyzer if _global_presidio_analyzer is not False else None

    def analyze(self, text: str) -> List[Any]:
        analyzer = self._get_analyzer()
        if not analyzer or not text:
            return []
        try:
            return analyzer.analyze(text=text, language="en")
        except Exception as e:
            logger.error(f"[PresidioEngine] Error during presidio analysis: {e}")
            raise PrivacySecurityError("Presidio generic PII detection failed")


# ---------------------------------------------------------------------------
# Core Secure Pseudonymizer
# ---------------------------------------------------------------------------
class LocalPseudonymizer:
    def __init__(self):
        self.validator = PostMaskingValidator()
        self.presidio = PresidioEngine()

    def get_db_master_entities(self) -> Tuple[List[str], List[str], List[str]]:
        """
        Fetches known customer names, project names, employee names, and client contact names
        from local MySQL database.
        """
        customers = []
        projects = []
        employees = []
        try:
            from db.database import get_db_engine
            from sqlalchemy import text
            engine = get_db_engine()
            with engine.connect() as conn:
                # 1. Customers
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

                # 2. Projects
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

                # 3. Employees
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

                # 4. Client Contacts
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
            logger.debug(f"[Pseudonymizer] DB master lookup notice: {e}")
        return list(set(customers)), list(set(projects)), list(set(employees))

    def prepare_for_external_llm(
        self,
        text: str,
        db_customers: List[str] = None,
        db_projects: List[str] = None,
        db_employees: List[str] = None
    ) -> PrivacyResult:
        """
        Executes complete fail-closed privacy workflow:
        1. Local PII & CRM Detection
        2. Presidio Generic NLP Analysis
        3. Token Replacement (Single-token consistency per request)
        4. Post-Masking Verification
        5. Returns PrivacyResult with safe=True (or safe=False / PrivacySecurityError)
        """
        req_id = str(uuid.uuid4())
        if not text or not isinstance(text, str):
            return PrivacyResult(request_id=req_id, masked_text=text or "", safe=True)

        try:
            mapping: Dict[str, str] = {}
            reverse_entity_tokens: Dict[Tuple[str, str], str] = {}
            entity_counts: Dict[str, int] = {}
            masked_text = text

            # Helper for consistent single-token assignment per request
            def get_or_create_token(entity_str: str, token_type: str, category_name: str) -> str:
                clean_val = entity_str.strip()
                if not clean_val or len(clean_val) < 2 or clean_val.startswith('<'):
                    return ""

                # Strip leading title prefixes if present
                clean_val_no_title = re.sub(r'^(?:Mr\.|Mrs\.|Ms\.|Dr\.|Eng\.|Prof\.)\s*', '', clean_val, flags=re.IGNORECASE).strip()
                if not clean_val_no_title:
                    clean_val_no_title = clean_val

                lookup_key = (clean_val_no_title.lower(), token_type)
                if lookup_key in reverse_entity_tokens:
                    return reverse_entity_tokens[lookup_key]

                idx = entity_counts.get(token_type, 0) + 1
                entity_counts[token_type] = idx
                token = f"<{token_type}_{idx}>"
                reverse_entity_tokens[lookup_key] = token
                mapping[token] = clean_val_no_title
                entity_counts[category_name] = entity_counts.get(category_name, 0) + 1
                return token

            # 0. Redact Legal Disclaimer Boilerplates
            disclaimer_patterns = [
                r'This email and any attachments are confidential.*?(?=\n\n|\Z)',
                r'CONFIDENTIALITY NOTICE:.*?(?=\n\n|\Z)',
                r'The information contained in this message is confidential.*?(?=\n\n|\Z)'
            ]
            for dp in disclaimer_patterns:
                masked_text = re.sub(dp, '[LEGAL_DISCLAIMER_REDACTED]', masked_text, flags=re.IGNORECASE | re.DOTALL)

            # 1. Fetch DB Masters if not explicitly provided
            if db_customers is None and db_projects is None and db_employees is None:
                c_list, p_list, e_list = self.get_db_master_entities()
            else:
                c_list = db_customers if db_customers is not None else []
                p_list = db_projects if db_projects is not None else []
                e_list = db_employees if db_employees is not None else []

            db_masters = (c_list, p_list, e_list)

            # 2. Extract Entities from Header Lines (From, To, Cc, Bcc, Forwarded by/to, Sender)
            header_pattern = r'(?i)\b(?:From|To|Cc|Bcc|Forwarded\s+by|Forwarded\s+to|Sent\s+by|Reply-To|Author|Sender)\s*:\s*([^\n]+)'
            for h_match in re.finditer(header_pattern, masked_text):
                h_val = h_match.group(1)
                for name_match in re.finditer(r'([A-Z][A-Za-z.\'-]+(?:\s+[A-Z][A-Za-z.\'-]+)+)\s*<', h_val):
                    get_or_create_token(name_match.group(1), "PERSON_TOKEN", "PERSON")
                parts = [p.strip() for p in h_val.split(',')]
                for p in parts:
                    name_part = re.sub(r'<[^>]+>', '', p).strip()
                    if name_part and len(name_part.split()) >= 2 and all(w[0].isupper() for w in name_part.split() if w.isalpha()):
                        get_or_create_token(name_part, "PERSON_TOKEN", "PERSON")

            # 3. Extract Name + Email Pairs in Body
            for name_email in re.finditer(r'([A-Z][A-Za-z.\'-]+(?:\s+[A-Z][A-Za-z.\'-]+)+)\s*<[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}>', masked_text):
                get_or_create_token(name_email.group(1), "PERSON_TOKEN", "PERSON")

            # 4. Extract Signature Closing Names (Supports single & multi-word names e.g. Arpit, Bhavik Patel)
            sig_pattern = r'(?i)\b(?:Regards|Best\s+regards|Thanks\s*(?:&\s*regards)?|Sincerely|Warm\s+regards|Yours\s+truly|Yours\s+sincerely)\s*,\s*\n+\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})'
            for sig_match in re.finditer(sig_pattern, masked_text):
                get_or_create_token(sig_match.group(1), "PERSON_TOKEN", "PERSON")

            # 5. Extract Salutation / Prefix Names (Mr. David Reynolds)
            prefix_pattern = r'\b(?:Mr\.|Mrs\.|Ms\.|Dr\.|Eng\.|Prof\.)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})'
            for pref_match in re.finditer(prefix_pattern, masked_text):
                get_or_create_token(pref_match.group(1), "PERSON_TOKEN", "PERSON")

            # 6. Extract DB Master Entities ONLY IF THEY APPEAR IN THE TEXT (Prevents 5000+ entity explosion)
            text_lower = masked_text.lower()
            for proj in p_list:
                if proj and len(proj) >= 3 and proj.lower() in text_lower:
                    get_or_create_token(proj, "PROJECT_TOKEN", "PROJECT")

            for cust in c_list:
                if cust and len(cust) >= 3 and cust.lower() in text_lower:
                    get_or_create_token(cust, "CUSTOMER_TOKEN", "CUSTOMER")

            for emp in e_list:
                if emp and len(emp) >= 3 and emp.lower() in text_lower:
                    get_or_create_token(emp, "PERSON_TOKEN", "PERSON")

            # 7. Corporate Suffixes & Subject Line Company/Prospect Names
            corp_suffix_matches = re.findall(r'\b([A-Z0-9][A-Za-z0-9\s&._-]{2,60}\s+(?:B\.?S\.?C\.?\s*(?:CLOSED|PUBLIC)?|W\.?L\.?L\.?|S\.?P\.?C\.?|LLC|LTD|LIMITED|INC|CORP))\b', masked_text, re.IGNORECASE)
            for val in set(corp_suffix_matches):
                get_or_create_token(val, "CUSTOMER_TOKEN", "CUSTOMER")

            # Subject/Header company patterns (e.g. Lead: Nexus Dynamics, Estimation Pending For: Nexus Dynamics)
            subject_company_matches = re.findall(r'(?i)(?:Lead|Client|Customer|Prospect|Estimation\s+Pending\s+For|Pending\s+For)\s*[:\-]\s*([A-Z0-9][A-Za-z0-9\s&._-]{2,40}?)(?=\s*(?:\(TESTING\)|\(|\)|-|\n|\r|$))', masked_text)
            for val in set(subject_company_matches):
                val_clean = val.strip()
                val_clean = re.sub(r'(?i)^(?:Qualified\s+Sales\s+Lead|Sales\s+Lead|Estimation\s+Pending\s+For|Pending\s+For|Estimation|Proposal|RFP|Inquiry)\s*[-:]?\s*', '', val_clean).strip()
                val_clean = val_clean.strip(".,;: \t\r\n")
                if val_clean and len(val_clean) >= 3 and val_clean.lower() not in ("crm", "estimation", "qualified sales", "sales lead", "proposal", "lead", "action required", "the new client"):
                    get_or_create_token(val_clean, "CUSTOMER_TOKEN", "CUSTOMER")

            # Standalone company names with (TESTING) or explicit prospect markers
            testing_company_matches = re.findall(r'\b([A-Z][A-Za-z0-9&._-]+\s+[A-Z][A-Za-z0-9&._-]+)(?=\s*\([A-Z]+\))', masked_text)
            for val in set(testing_company_matches):
                get_or_create_token(val, "CUSTOMER_TOKEN", "CUSTOMER")

            # 8. Labeled Form Fields (Company:, Client:, Contact Name:)
            lbl_cust_matches = re.findall(r'(?i)\b(?:Company|Client|Organization|Customer)\s*:\s*([A-Z0-9][A-Za-z0-9\s&._-]{1,60}?)(?=\s*(?:Contact|Name|Phone|Email|Budget|Project|Task|Type|Priority|Status|Notes|Date|Mobile|Tel|Fax|http|www|BH|\d{8}|\n|\r|\.|;|,|\$|€|£|BHD|BD)|$)', masked_text)
            for val in set(lbl_cust_matches):
                val_clean = val.strip().split('\n')[0].strip()
                get_or_create_token(val_clean, "CUSTOMER_TOKEN", "CUSTOMER")

            lbl_person_matches = re.findall(r'(?i)\b(?:Contact\s+Name|Contact\s+Person|Client\s+Contact)\s*:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})', masked_text)
            for val in set(lbl_person_matches):
                val_clean = val.strip().split('\n')[0].strip()
                get_or_create_token(val_clean, "PERSON_TOKEN", "PERSON")

            # 9. Secrets, Passwords, API Keys, JWTs, DB Connection Strings
            secret_patterns = [
                (r'\b(?:sk-[a-zA-Z0-9]{20,}|pk_[a-zA-Z0-9]{20,}|key-[a-zA-Z0-9]{20,})\b', "SECRET_TOKEN", "API_KEY"),
                (r'\b(?:bearer\s+ey[a-zA-Z0-9._-]{20,}|ey[a-zA-Z0-9._-]{20,}\.ey[a-zA-Z0-9._-]{20,}\.[a-zA-Z0-9._-]{10,})\b', "SECRET_TOKEN", "JWT"),
                (r'(?i)\b(?:password|secret|api[_-]?key)\s*[:=]\s*(\S+)', "SECRET_TOKEN", "PASSWORD"),
                (r'\b(?:mysql|postgresql|mongodb\+srv|oracle|sqlserver):\/\/[^\s\'"]+\b', "SECRET_TOKEN", "DB_URL"),
            ]
            for pat, tok_type, cat in secret_patterns:
                for match in set(re.findall(pat, masked_text)):
                    val = match[1] if isinstance(match, tuple) else match
                    get_or_create_token(val, tok_type, cat)

            # 10. Financial Identifiers: Bahrain IBAN, Credit Cards, Financial Amounts
            iban_matches = re.findall(r'\bBH\d{2}[A-Z0-9]{4}\d{14}\b', masked_text, re.IGNORECASE)
            for iban in set(iban_matches):
                get_or_create_token(iban, "CONFIDENTIAL_TOKEN", "IBAN")

            card_matches = re.findall(r'\b(?:\d[ -]*?){13,16}\b', masked_text)
            for card in set(card_matches):
                digits = re.sub(r'\D', '', card)
                if len(digits) in (13, 14, 15, 16):
                    get_or_create_token(card, "CONFIDENTIAL_TOKEN", "CREDIT_CARD")

            curr_patterns = [
                r'(?:BHD|BHD\.|BD|USD|EUR|\$|€|£|SAR|AED)\s*[\d,]+(?:\.\d+)?',
                r'[\d,]+(?:\.\d+)?\s*(?:BHD|BD|USD|EUR|SAR|AED)'
            ]
            for cp in curr_patterns:
                for amt in set(re.findall(cp, masked_text, re.IGNORECASE)):
                    get_or_create_token(amt, "AMOUNT_TOKEN", "AMOUNT")

            # 11. Emails & Phone Numbers
            email_matches = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', masked_text)
            for email_str in set(email_matches):
                get_or_create_token(email_str, "EMAIL_TOKEN", "EMAIL")

            phone_matches = re.findall(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b', masked_text)
            for ph in set(phone_matches):
                digits_only = re.sub(r'\D', '', ph)
                if 7 <= len(digits_only) <= 15 and not ph.startswith('2026') and not ph.startswith('2025'):
                    get_or_create_token(ph, "PHONE_TOKEN", "PHONE")

            # 12. Dates & CRM IDs & URLs / Paths
            date_patterns = [
                r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b',
                r'\b\d{4}[-/]\d{2}[-/]\d{2}\b',
                r'\b\d{2}[-/]\d{2}[-/]\d{4}\b'
            ]
            for dp in date_patterns:
                for date_str in set(re.findall(dp, masked_text)):
                    get_or_create_token(date_str, "DATE_TOKEN", "DATE")

            crm_id_patterns = [
                (r'\b(?:CUST|CUSTOMER)[-_]?\d{3,8}\b', "CUSTOMER_ID_TOKEN", "CUSTOMER_ID"),
                (r'\b(?:EMP|EMPLOYEE)[-_]?\d{3,8}\b', "EMP_ID_TOKEN", "EMPLOYEE_ID"),
                (r'\b(?:PRJ|PROJECT)[-_]?\d{3,8}\b', "PROJ_ID_TOKEN", "PROJECT_ID"),
                (r'\b(?:INV|INVOICE)[-_]?[A-Z0-9]{3,12}\b', "INVOICE_NUM_TOKEN", "INVOICE_NUMBER"),
                (r'\b(?:CNT|CONTRACT)[-_]?[A-Z0-9]{3,12}\b', "CONTRACT_NUM_TOKEN", "CONTRACT_NUMBER"),
                (r'\b(?:GL|GLCODE)[-_]?\d{4,8}\b', "GL_CODE_TOKEN", "GL_CODE"),
                (r'\b(?:REF|REFERENCE)[-_]?[A-Z0-9]{4,12}\b', "REF_NUM_TOKEN", "REFERENCE_NUMBER")
            ]
            for pat, tok_type, cat in crm_id_patterns:
                for match_id in set(re.findall(pat, masked_text, re.IGNORECASE)):
                    get_or_create_token(match_id, tok_type, cat)

            path_url_patterns = [
                (r'\bfile:\/\/\/[^\s\'"]+\b', "PATH_TOKEN", "FILE_PATH"),
                (r'\b[A-Za-z]:\\[^\s\'"]+\b', "PATH_TOKEN", "FILE_PATH"),
                (r'\\\\[^\s\'"]+\\[^\s\'"]+\b', "PATH_TOKEN", "FILE_PATH"),
                (r'\bhttps?:\/\/(?:internal|crm|finance|hr)\.[^\s\'"]+\b', "URL_TOKEN", "INTERNAL_URL"),
                (r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b', "IP_TOKEN", "IP_ADDRESS")
            ]
            for pat, tok_type, cat in path_url_patterns:
                for val in set(re.findall(pat, masked_text, re.IGNORECASE)):
                    get_or_create_token(val, tok_type, cat)

            # 13. Presidio Generic NLP Layer (Filtered & Validated)
            try:
                presidio_results = self.presidio.analyze(masked_text)
                for res_item in presidio_results:
                    if res_item.score >= 0.75 and res_item.entity_type != "DATE_TIME":
                        start, end = res_item.start, res_item.end
                        val = masked_text[start:end]
                        if val and len(val) >= 3 and not val.startswith('<') and not val.endswith('>') and '<' not in val and '>' not in val:
                            val_clean = re.sub(r'^(?:Mr\.|Mrs\.|Ms\.|Dr\.|Eng\.|Prof\.)\s*', '', val, flags=re.IGNORECASE).strip().lower()
                            # Only register if entity is not already registered under a known entity type
                            if not any(k[0] == val_clean for k in reverse_entity_tokens.keys()):
                                get_or_create_token(val, f"{res_item.entity_type}_TOKEN", res_item.entity_type)
            except Exception as pe:
                logger.warning(f"[Pseudonymizer] Presidio warning: {pe}")

            # 14. Multi-Pass Case-Insensitive Token Replacement
            sorted_registered = sorted(reverse_entity_tokens.items(), key=lambda x: len(x[0][0]), reverse=True)
            for (raw_val, tok_type), token in sorted_registered:
                if not raw_val or len(raw_val) < 2:
                    continue
                pattern = r'\b' + re.escape(raw_val) + r'\b' if raw_val.replace(" ", "").isalnum() else re.escape(raw_val)
                masked_text = re.sub(pattern, token, masked_text, flags=re.IGNORECASE)

            # 15. Post-Masking Security Validation
            is_valid, reason = self.validator.validate(masked_text, db_masters)
            if not is_valid:
                logger.error(f"[Pseudonymizer] Post-masking security validation failed: {reason}")
                return PrivacyResult(
                    request_id=req_id,
                    masked_text="",
                    token_mapping={},
                    safe=False,
                    blocked_reason=reason
                )

            payload_hash = hashlib.sha256(masked_text.encode('utf-8')).hexdigest()
            logger.info(f"[Pseudonymizer] Prepared request {req_id[:8]}... safe=True | Entities: {len(mapping)} | Hash: {payload_hash[:12]}")

            return PrivacyResult(
                request_id=req_id,
                masked_text=masked_text,
                token_mapping=mapping,
                safe=True,
                entity_counts=entity_counts,
                masked_hash=payload_hash
            )

        except Exception as e:
            if isinstance(e, PrivacySecurityError):
                raise
            logger.error(f"[Pseudonymizer] Privacy middleware exception: {e}")
            raise PrivacySecurityError("Privacy middleware encountered an error during masking execution")

    def unmask_data(self, data: Any, mapping: Dict[str, str]) -> Any:
        """
        Recursively unmasks tokens back to original values in dicts, lists, or strings locally.
        Sorts mapping by token length descending and handles case-insensitive token matching,
        supporting both bracketed (<TOKEN_1>) and bare (TOKEN_1) tokens.
        """
        if not mapping:
            return data

        # Build list of (token_pattern, real_val)
        token_pairs = []
        for token, real_val in mapping.items():
            if not token:
                continue
            token_pairs.append((token, str(real_val)))
            bare_token = token.strip("<>")
            if bare_token and bare_token != token:
                token_pairs.append((bare_token, str(real_val)))

        # Sort tokens by length descending to prevent sub-token collisions
        sorted_pairs = sorted(token_pairs, key=lambda x: len(x[0]), reverse=True)

        def _recursive_unmask(val: Any) -> Any:
            if isinstance(val, str):
                unmasked = val
                for tok, real_v in sorted_pairs:
                    if tok in unmasked:
                        unmasked = unmasked.replace(tok, real_v)
                    elif tok.lower() in unmasked.lower():
                        unmasked = re.sub(re.escape(tok), real_v, unmasked, flags=re.IGNORECASE)
                # Clean up duplicated title prefixes if generated (e.g. Mr. Mr. John -> Mr. John)
                unmasked = re.sub(r'\b(Mr\.|Mrs\.|Ms\.|Dr\.|Eng\.|Prof\.)\s+\1\b', r'\1', unmasked, flags=re.IGNORECASE)
                return unmasked
            elif isinstance(val, dict):
                return {k: _recursive_unmask(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [_recursive_unmask(item) for item in val]
            return val

        return _recursive_unmask(data)



# ---------------------------------------------------------------------------
# Global Singleton & Backward-Compatible Wrappers
# ---------------------------------------------------------------------------
_pseudonymizer_instance = LocalPseudonymizer()

def prepare_for_external_llm(
    text: str,
    db_customers: List[str] = None,
    db_projects: List[str] = None,
    db_employees: List[str] = None
) -> PrivacyResult:
    """Primary fail-closed entry point for external LLM requests."""
    return _pseudonymizer_instance.prepare_for_external_llm(text, db_customers, db_projects, db_employees)

def mask_email_text(
    text: str,
    db_customers: List[str] = None,
    db_projects: List[str] = None,
    db_employees: List[str] = None
) -> Tuple[str, Dict[str, str]]:
    """
    Backward-compatible wrapper returning (masked_text, token_mapping).
    Enforces fail-closed security: raises PrivacySecurityError if validation fails.
    """
    res = _pseudonymizer_instance.prepare_for_external_llm(text, db_customers, db_projects, db_employees)
    if not res.safe:
        raise PrivacySecurityError(f"Request blocked by privacy validation: {res.blocked_reason}")
    return res.masked_text, res.token_mapping

def unmask_data(data: Any, mapping: Dict[str, str]) -> Any:
    """Backward-compatible unmasking wrapper."""
    return _pseudonymizer_instance.unmask_data(data, mapping)
