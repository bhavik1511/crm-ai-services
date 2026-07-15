"""
MCP Client Tools for CRM AI Agent.

Provides LangChain-compatible tools that give the AI agent direct, 
read-only access to the live MySQL database schema and data.

This replaces the static hardcoded schema in the SQL prompt with
dynamic, real-time database exploration capabilities.

Security: ALL tools are strictly READ-ONLY. Any mutating SQL
(INSERT, UPDATE, DELETE, DROP, ALTER, etc.) is blocked.
"""
import re
import os
import json
from typing import Optional
from dotenv import load_dotenv
from sqlalchemy import text, inspect
from langchain_core.tools import tool
from db.database import get_db_engine

load_dotenv()

# ---------------------------------------------------------------------------
# Safety — block any mutating SQL
# ---------------------------------------------------------------------------
DANGEROUS_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE|"
    r"EXEC|EXECUTE|GRANT|REVOKE|CALL|LOCK|UNLOCK|RENAME|LOAD|INTO\s+OUTFILE)\b",
    re.IGNORECASE,
)


def _is_safe_query(sql: str) -> bool:
    """Returns True if the SQL is a safe read-only query."""
    return not DANGEROUS_SQL.search(sql)


# ---------------------------------------------------------------------------
# Tool 1: List all tables in the database
# ---------------------------------------------------------------------------
@tool
def mcp_list_tables() -> str:
    """List ALL tables in the CRM MySQL database.
    Use this tool FIRST to discover what tables are available before writing any SQL query.
    Returns a JSON array of table names.
    """
    try:
        engine = get_db_engine()
        inspector = inspect(engine)
        tables = sorted(inspector.get_table_names())
        return json.dumps({
            "database": os.getenv("DB_NAME", "dashboard_ai"),
            "table_count": len(tables),
            "tables": tables
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Failed to list tables: {str(e)}"})


# ---------------------------------------------------------------------------
# Tool 2: Describe a specific table's schema
# ---------------------------------------------------------------------------
@tool
def mcp_describe_table(table_name: str) -> str:
    """Describe the schema of a specific table in the CRM MySQL database.
    Returns column names, types, nullable flags, primary keys, and foreign key relationships.
    Use this tool to understand a table's structure BEFORE writing SQL queries against it.
    
    Args:
        table_name: The exact name of the table to describe (e.g., 'invoice', 'saleslead', 'employees')
    """
    try:
        engine = get_db_engine()
        inspector = inspect(engine)
        
        # Check if table exists
        all_tables = inspector.get_table_names()
        if table_name not in all_tables:
            # Fuzzy match suggestion
            suggestions = [t for t in all_tables if table_name.lower() in t.lower()]
            return json.dumps({
                "error": f"Table '{table_name}' not found.",
                "suggestions": suggestions[:5],
                "hint": "Use mcp_list_tables to see all available tables."
            })
        
        # Get columns
        columns = inspector.get_columns(table_name)
        column_info = []
        for col in columns:
            column_info.append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": col.get("nullable", True),
                "default": str(col.get("default", "")) if col.get("default") else None,
                "autoincrement": col.get("autoincrement", False),
            })
        
        # Get primary keys
        pk = inspector.get_pk_constraint(table_name)
        pk_columns = pk.get("constrained_columns", []) if pk else []
        
        # Get foreign keys
        fks = inspector.get_foreign_keys(table_name)
        fk_info = []
        for fk in fks:
            fk_info.append({
                "columns": fk.get("constrained_columns", []),
                "references_table": fk.get("referred_table", ""),
                "references_columns": fk.get("referred_columns", []),
            })
        
        # Get indexes
        indexes = inspector.get_indexes(table_name)
        index_info = []
        for idx in indexes:
            index_info.append({
                "name": idx.get("name", ""),
                "columns": idx.get("column_names", []),
                "unique": idx.get("unique", False),
            })
        
        # Get row count (approximate)
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`"))
            row_count = result.scalar()
        
        # Get sample data (first 3 rows)
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM `{table_name}` LIMIT 3"))
            sample_rows = [dict(row._mapping) for row in result]
            # Convert non-serializable values
            for row in sample_rows:
                for k, v in row.items():
                    if not isinstance(v, (str, int, float, bool, type(None))):
                        row[k] = str(v)
        
        return json.dumps({
            "table": table_name,
            "row_count": row_count,
            "columns": column_info,
            "primary_keys": pk_columns,
            "foreign_keys": fk_info,
            "indexes": index_info,
            "sample_data": sample_rows,
        }, indent=2, default=str)
        
    except Exception as e:
        return json.dumps({"error": f"Failed to describe table '{table_name}': {str(e)}"})


# ---------------------------------------------------------------------------
# Tool 3: Execute a READ-ONLY SQL query
# ---------------------------------------------------------------------------
@tool
def mcp_read_query(sql: str) -> str:
    """Execute a READ-ONLY SQL query against the CRM MySQL database and return results.
    ONLY SELECT queries are allowed. Any INSERT, UPDATE, DELETE, DROP, ALTER, or other 
    mutating queries will be BLOCKED for security.
    
    Results are limited to 100 rows maximum.
    
    Args:
        sql: A valid MySQL SELECT query to execute.
    """
    if not sql or not sql.strip():
        return json.dumps({"error": "Empty SQL query provided."})
    
    # Strip markdown code blocks if the LLM wraps the SQL
    sql = re.sub(r"^```(?:sql)?\s*", "", sql.strip(), flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql.strip())
    sql = sql.strip().rstrip(";")
    
    # Safety check
    if not _is_safe_query(sql):
        return json.dumps({
            "error": "BLOCKED: Only SELECT (read-only) queries are permitted.",
            "blocked_sql": sql[:200],
        })
    
    # Ensure it starts with SELECT (or WITH for CTEs)
    sql_upper = sql.strip().upper()
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH") or sql_upper.startswith("SHOW") or sql_upper.startswith("DESCRIBE") or sql_upper.startswith("EXPLAIN")):
        return json.dumps({
            "error": "Only SELECT, SHOW, DESCRIBE, and EXPLAIN queries are allowed.",
            "provided_start": sql_upper[:30],
        })
    
    # Add LIMIT if not present
    if "LIMIT" not in sql.upper():
        sql += " LIMIT 100"
    
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = result.fetchall()
            
            # Convert to list of dicts for clean JSON output
            data = []
            for row in rows:
                row_dict = {}
                for i, col in enumerate(columns):
                    val = row[i]
                    if not isinstance(val, (str, int, float, bool, type(None))):
                        val = str(val)
                    row_dict[col] = val
                data.append(row_dict)
            
            return json.dumps({
                "query": sql,
                "row_count": len(data),
                "columns": columns,
                "data": data,
            }, indent=2, default=str)
            
    except Exception as e:
        return json.dumps({
            "error": f"SQL execution failed: {str(e)}",
            "query": sql[:200],
            "hint": "Check table/column names using mcp_describe_table first.",
        })


# ---------------------------------------------------------------------------
# Cached doc for mcp_search_docs (loaded once, reused across calls)
# ---------------------------------------------------------------------------
_mcp_cached_doc = None
_mcp_cached_sections = None


def _get_mcp_doc_sections():
    """Load and cache the master reference document, split into sections."""
    global _mcp_cached_doc, _mcp_cached_sections
    if _mcp_cached_sections is not None:
        return _mcp_cached_sections, None

    docs_dir = os.path.dirname(os.path.abspath(__file__))
    master_file = os.path.join(docs_dir, "ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.md")

    try:
        with open(master_file, "r", encoding="utf-8") as f:
            _mcp_cached_doc = f.read()
    except FileNotFoundError:
        return None, f"Documentation file not found: {master_file}"

    if not _mcp_cached_doc:
        return None, "Documentation file is empty."

    # Split into sections by ## headers (preserving the header in each section)
    raw_sections = re.split(r'\n(?=## )', _mcp_cached_doc)
    parsed = []
    for section in raw_sections:
        lines = section.strip().split("\n", 1)
        title = lines[0].strip() if lines else ""
        parsed.append((title, section.strip()))
    _mcp_cached_sections = parsed
    return _mcp_cached_sections, None


# ---------------------------------------------------------------------------
# Tool 4: Search the CRM backend documentation for business logic
# ---------------------------------------------------------------------------
@tool
def mcp_search_docs(query: str) -> str:
    """Search the ANTIGRAVITY CRM MASTER REFERENCE (278KB, 189 tables, all formulas & rules)
    for specific business logic, formulas, validation rules, workflow details, approval chains,
    hardcoded IDs, SQL patterns, or system behavior.

    Use this tool when the user asks about HOW something is calculated, what business
    rules apply, what validation logic exists, what approval chain is used, what hardcoded
    IDs exist, or how a CRM feature works internally.

    This searches the DEFINITIVE reference built from 836 TypeScript files + full production SQL dump.

    Args:
        query: Keywords to search for (e.g., 'leave calculation GOSI', 'cash advance approval chain',
               'invoice number generation', 'final settlement formula', 'project status transitions')
    """
    try:
        sections, error = _get_mcp_doc_sections()
        if error:
            return json.dumps({"error": error})

        query_lower = query.lower()
        keywords = [kw.strip() for kw in query_lower.split() if len(kw.strip()) > 2]

        if not keywords:
            return json.dumps({"error": f"No valid search keywords in: {query}"})

        matching_sections = []
        for title, content in sections:
            title_lower = title.lower()
            content_lower = content.lower()
            # Title matches count double for relevance
            title_score = sum(2 for kw in keywords if kw in title_lower)
            content_score = sum(1 for kw in keywords if kw in content_lower)
            score = title_score + content_score

            if score >= max(1, len(keywords) // 2):
                display = content
                if len(display) > 3000:
                    display = display[:3000] + "\n... [truncated — use more specific keywords]"
                matching_sections.append((score, title, display))

        if not matching_sections:
            return json.dumps({"message": f"No documentation found matching: {query}", "keywords_searched": keywords})

        matching_sections.sort(key=lambda x: x[0], reverse=True)
        results = matching_sections[:5]

        output_sections = []
        for score, title, section in results:
            output_sections.append({"relevance_score": score, "section_title": title, "content": section})

        return json.dumps({
            "query": query,
            "source": "ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.md (278KB, 189 tables, 64 sections)",
            "results_found": len(results),
            "sections": output_sections,
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": f"Documentation search failed: {str(e)}"})


# ---------------------------------------------------------------------------
# Tool 5: Get distinct values for a column (Data Profiling / ENUM Discovery)
# ---------------------------------------------------------------------------
@tool
def mcp_get_column_distinct_values(table_name: str, column_name: str) -> str:
    """Get the distinct (unique) values for a specific column in a table.
    Use this tool for data profiling: when you need to know exactly what 'status', 'type',
    or 'category' values actually exist in the live database before writing a SQL query.
    
    Args:
        table_name: The exact name of the table (e.g., 'm_leadstatus', 'projects')
        column_name: The exact name of the column (e.g., 'name', 'status')
    """
    try:
        engine = get_db_engine()
        # Limit to 100 to prevent massive payloads for high-cardinality columns
        with engine.connect() as conn:
            query = text(f"SELECT DISTINCT `{column_name}` FROM `{table_name}` WHERE `{column_name}` IS NOT NULL LIMIT 100")
            result = conn.execute(query)
            
            distinct_values = [str(row[0]) for row in result]
            
            return json.dumps({
                "table": table_name,
                "column": column_name,
                "total_unique_returned": len(distinct_values),
                "values": distinct_values
            }, indent=2)
            
    except Exception as e:
        return json.dumps({"error": f"Failed to get distinct values for {table_name}.{column_name}: {str(e)}"})


# ---------------------------------------------------------------------------
# Tool 6: Fetch a single record by primary key ID
# ---------------------------------------------------------------------------
@tool
def mcp_get_record_by_id(table_name: str, record_id: int) -> str:
    """Fetch a single record from any CRM table by its primary key ID.
    Use this instead of writing a SELECT query when you know the exact record ID.
    Much faster than mcp_read_query for single-record lookups.
    Args:
        table_name: Table name (e.g. 'invoice', 'proposal', 'projects', 'saleslead', 'customers')
        record_id: The integer primary key value
    """
    if not _is_safe_query(f"SELECT * FROM `{table_name}` WHERE id = {record_id}"):
        return json.dumps({"error": "Blocked"})
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM `{table_name}` WHERE id = :rid LIMIT 1"), {"rid": record_id})
            row = result.fetchone()
            if not row:
                return json.dumps({"error": f"No record found in {table_name} with id={record_id}"})
            columns = list(result.keys())
            data = {}
            for i, col in enumerate(columns):
                val = row[i]
                data[col] = str(val) if not isinstance(val, (str, int, float, bool, type(None))) else val
            return json.dumps({"table": table_name, "record_id": record_id, "data": data}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool 7: Get statistical profile for numeric columns in a table
# ---------------------------------------------------------------------------
@tool
def mcp_get_table_stats(table_name: str, where_clause: Optional[str] = None) -> str:
    """Get MIN, MAX, AVG, SUM, COUNT for all numeric columns in a table.
    Use this to understand data ranges before writing comparison or threshold queries.
    Args:
        table_name: Table to profile (e.g. 'invoice', 'proposal')
        where_clause: Optional SQL WHERE condition (e.g. "is_active = 1 AND payment_status_id = 2")
    """
    if where_clause and DANGEROUS_SQL.search(where_clause):
        return json.dumps({"error": "BLOCKED: Dangerous SQL in where_clause"})
    try:
        engine = get_db_engine()
        inspector = inspect(engine)
        columns = inspector.get_columns(table_name)
        numeric_types = ('INTEGER', 'BIGINT', 'SMALLINT', 'DECIMAL', 'FLOAT', 'DOUBLE', 'NUMERIC', 'INT')
        numeric_cols = [col['name'] for col in columns if any(t in str(col['type']).upper() for t in numeric_types)]

        if not numeric_cols:
            return json.dumps({"message": f"No numeric columns found in {table_name}"})

        stats_expr = ", ".join(
            f"MIN(`{c}`) AS `{c}_min`, MAX(`{c}`) AS `{c}_max`, ROUND(AVG(`{c}`),2) AS `{c}_avg`, ROUND(SUM(`{c}`),2) AS `{c}_sum`"
            for c in numeric_cols[:10]  # limit to first 10 to avoid huge queries
        )
        where = f"WHERE {where_clause}" if where_clause else ""
        sql = f"SELECT COUNT(*) AS total_rows, {stats_expr} FROM `{table_name}` {where}"

        with engine.connect() as conn:
            result = conn.execute(text(sql))
            row = result.fetchone()
            keys = list(result.keys())
            data = {
                keys[i]: (
                    float(row[i]) if row[i] is not None and isinstance(row[i], (int, float)) else
                    str(row[i]) if row[i] is not None else None
                )
                for i in range(len(keys))
            }

        return json.dumps({"table": table_name, "where": where_clause, "stats": data}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool 8: Full-text search across all major CRM entity tables simultaneously
# ---------------------------------------------------------------------------
@tool
def mcp_search_records(search_term: str) -> str:
    """Full-text search for a term across all major CRM entity tables simultaneously.
    Use this when the user references a name, company, code, or identifier without specifying
    which table it belongs to. Returns matches from customers, projects, employees, proposals, and leads.
    Args:
        search_term: The text to search for (e.g. 'Al Baraka', 'GT-2024', 'Ahmed Al')
    """
    safe_term = search_term.replace("'", "''").replace(";", "")[:100]
    queries = {
        "customers": f"SELECT id, customer_name AS name, cust_code AS code, 'customer' AS entity_type FROM customers WHERE is_active = 1 AND (customer_name LIKE '%{safe_term}%' OR cust_code LIKE '%{safe_term}%') LIMIT 5",
        "projects": f"SELECT id, name, code, 'project' AS entity_type FROM projects WHERE is_active = 1 AND (name LIKE '%{safe_term}%' OR code LIKE '%{safe_term}%') LIMIT 5",
        "employees": f"SELECT id, CONCAT(first_name, ' ', last_name) AS name, emp_email AS code, 'employee' AS entity_type FROM employees WHERE is_active = 1 AND (first_name LIKE '%{safe_term}%' OR last_name LIKE '%{safe_term}%' OR emp_email LIKE '%{safe_term}%') LIMIT 5",
        "proposals": f"SELECT id, code AS name, ref_no AS code, 'proposal' AS entity_type FROM proposal WHERE is_active = 1 AND (code LIKE '%{safe_term}%' OR ref_no LIKE '%{safe_term}%') LIMIT 5",
    }
    try:
        engine = get_db_engine()
        results = {}
        total_found = 0
        with engine.connect() as conn:
            for entity, sql in queries.items():
                rows = conn.execute(text(sql)).fetchall()
                data = [{"id": r[0], "name": r[1], "code": r[2], "entity_type": r[3]} for r in rows]
                if data:
                    results[entity] = data
                    total_found += len(data)

        if not results:
            return json.dumps({"message": f"No records found matching '{search_term}' across any table."})

        return json.dumps({
            "search_term": search_term,
            "total_matches": total_found,
            "results": results,
            "hint": "Use mcp_get_record_by_id or get_entity_profile for detailed data on any of these matches."
        }, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Convenience: Export all MCP tools as a list for the agent
# ---------------------------------------------------------------------------
ALL_MCP_TOOLS = [
    mcp_list_tables,
    mcp_describe_table,
    mcp_read_query,
    mcp_search_docs,
    mcp_get_column_distinct_values,
    mcp_get_record_by_id,      # NEW
    mcp_get_table_stats,       # NEW
    mcp_search_records,        # NEW
]

# Quick self-test
if __name__ == "__main__":
    print("=== MCP Client Tools Self-Test ===\n")

    print("1. Listing tables...")
    tables_result = mcp_list_tables.invoke({})
    parsed = json.loads(tables_result)
    print(f"   Found {parsed.get('table_count', 0)} tables")
    print(f"   Tables: {parsed.get('tables', [])[:10]}...\n")

    print("2. Describing 'invoice' table...")
    desc_result = mcp_describe_table.invoke({"table_name": "invoice"})
    parsed = json.loads(desc_result)
    print(f"   Columns: {len(parsed.get('columns', []))}")
    print(f"   Row count: {parsed.get('row_count', 0)}\n")

    print("3. Running a read-only query...")
    query_result = mcp_read_query.invoke({"sql": "SELECT COUNT(*) as total FROM invoice WHERE is_active = 1"})
    parsed = json.loads(query_result)
    print(f"   Result: {parsed.get('data', [])}\n")

    print("4. Searching documentation...")
    doc_result = mcp_search_docs.invoke({"query": "leave calculation GOSI"})
    parsed = json.loads(doc_result)
    print(f"   Found {parsed.get('results_found', 0)} matching sections\n")

    print("5. Testing mcp_search_records...")
    sr_result = mcp_search_records.invoke({"search_term": "Al Baraka"})
    parsed = json.loads(sr_result)
    print(f"   Total matches: {parsed.get('total_matches', 0)}\n")

    print("=== Self-Test Complete ===")
