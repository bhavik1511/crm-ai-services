"""
schema_intelligence.py — Auto-loads the full database schema at startup.

Queries INFORMATION_SCHEMA to build a complete, cached knowledge base
of every table, column, relationship, and sample data in the CRM database.
This is injected into the AI ad-hoc prompt so GPT-4o never needs to 
discover schema dynamically (faster + more accurate).

READ-ONLY: Only uses SELECT on INFORMATION_SCHEMA and LIMIT 3 samples.
"""

import os
import json
import time
import threading
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from db.database import get_db_engine

_schema_cache: Optional[str] = None
_schema_cache_time: float = 0
_CACHE_TTL_SECONDS = 1800  # 30 minutes
_lock = threading.Lock()


def _build_schema_snapshot() -> str:
    """Query INFORMATION_SCHEMA and build a comprehensive schema description."""
    engine = get_db_engine()
    lines = []
    
    with engine.connect() as conn:
        # 1. Get all tables with row counts
        tables_result = conn.execute(text("""
            SELECT TABLE_NAME, TABLE_ROWS, TABLE_COMMENT
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """))
        tables = [dict(row._mapping) for row in tables_result]
        
        lines.append(f"# DATABASE SCHEMA — {len(tables)} tables")
        lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        
        # 2. Get all foreign key relationships
        fk_result = conn.execute(text("""
            SELECT 
                TABLE_NAME, COLUMN_NAME,
                REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY TABLE_NAME, COLUMN_NAME
        """))
        fk_map = {}
        for row in fk_result:
            r = dict(row._mapping)
            key = r['TABLE_NAME']
            if key not in fk_map:
                fk_map[key] = []
            fk_map[key].append(r)
        
        # 3. Build per-table details
        for tbl in tables:
            table_name = tbl['TABLE_NAME']
            row_count = tbl['TABLE_ROWS'] or 0
            comment = tbl['TABLE_COMMENT'] or ''
            
            lines.append(f"## {table_name}  (~{row_count} rows)")
            if comment:
                lines.append(f"   Comment: {comment}")
            
            # Get columns
            cols_result = conn.execute(text(f"""
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, COLUMN_COMMENT
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tbl
                ORDER BY ORDINAL_POSITION
            """), {"tbl": table_name})
            
            cols = [dict(row._mapping) for row in cols_result]
            col_lines = []
            for c in cols:
                parts = [f"  - {c['COLUMN_NAME']} ({c['DATA_TYPE']})"]
                if c['COLUMN_KEY'] == 'PRI':
                    parts.append("[PK]")
                if c['COLUMN_KEY'] == 'MUL':
                    parts.append("[FK]")
                if c['IS_NULLABLE'] == 'NO':
                    parts.append("[NOT NULL]")
                if c.get('COLUMN_COMMENT'):
                    parts.append(f"// {c['COLUMN_COMMENT']}")
                col_lines.append(" ".join(parts))
            
            lines.extend(col_lines)
            
            # Show foreign keys
            if table_name in fk_map:
                lines.append(f"  RELATIONSHIPS:")
                for fk in fk_map[table_name]:
                    lines.append(f"    {fk['COLUMN_NAME']} → {fk['REFERENCED_TABLE_NAME']}.{fk['REFERENCED_COLUMN_NAME']}")
            
            # Get 3 sample rows (safely)
            try:
                sample_result = conn.execute(text(f"SELECT * FROM `{table_name}` LIMIT 3"))
                sample_rows = [dict(row._mapping) for row in sample_result]
                if sample_rows:
                    lines.append(f"  SAMPLE DATA ({len(sample_rows)} rows):")
                    col_names = list(sample_rows[0].keys())
                    # Truncate long values for prompt size
                    for sr in sample_rows:
                        preview = {}
                        for k, v in sr.items():
                            sv = str(v) if v is not None else "NULL"
                            preview[k] = sv[:60] + "..." if len(sv) > 60 else sv
                        lines.append(f"    {json.dumps(preview, default=str)}")
            except Exception:
                pass  # Skip tables with access issues
            
            lines.append("")
    
    return "\n".join(lines)


def get_schema_prompt() -> str:
    """
    Returns the full schema as a formatted string, using a cache.
    Safe to call from any thread. Auto-refreshes every 30 minutes.
    """
    global _schema_cache, _schema_cache_time
    
    now = time.time()
    if _schema_cache and (now - _schema_cache_time) < _CACHE_TTL_SECONDS:
        return _schema_cache
    
    with _lock:
        # Double-check after acquiring lock
        if _schema_cache and (time.time() - _schema_cache_time) < _CACHE_TTL_SECONDS:
            return _schema_cache
        
        try:
            print("[SchemaIntelligence] Building schema snapshot...")
            _schema_cache = _build_schema_snapshot()
            _schema_cache_time = time.time()
            
            # Save to file for debugging
            debug_path = os.path.join(os.path.dirname(__file__), "schema_snapshot.txt")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(_schema_cache)
            
            table_count = _schema_cache.count("## ")
            print(f"[SchemaIntelligence] Schema loaded: {table_count} tables, {len(_schema_cache)} chars")
            return _schema_cache
        except Exception as e:
            print(f"[SchemaIntelligence] ERROR: {e}")
            return "# Schema unavailable — use mcp_list_tables and mcp_describe_table to discover schema dynamically."


def get_schema_summary() -> str:
    """Returns a shorter summary with just table names and row counts."""
    engine = get_db_engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT TABLE_NAME, TABLE_ROWS
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """))
        lines = ["Available tables:"]
        for row in result:
            r = dict(row._mapping)
            lines.append(f"  - {r['TABLE_NAME']} (~{r['TABLE_ROWS'] or 0} rows)")
        return "\n".join(lines)
