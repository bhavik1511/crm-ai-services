# Enterprise Planner Validation Report
**Date**: 2026-07-21 15:28:09

## Summary Metrics
- **Total Tests**: 11
- **Passed**: 0 (0.0%)
- **Failed**: 11

## Detailed Results
| Test ID | Category | Query | Status | Capability | Confidence | Time (ms) |
|---|---|---|---|---|---|---|
| REV-001 | Revenue | Show revenue this month | ❌ FAIL | None | 0.0 | 134.21 |
| REV-002 | Revenue | Top 10 customers by revenue | ❌ FAIL | None | 0.0 | 61.99 |
| REV-003 | Revenue | Compare FY2025 vs FY2024 | ❌ FAIL | None | 0.0 | 62.08 |
| KPI-001 | KPI | KPI report | ❌ FAIL | None | 0.0 | 194.69 |
| KPI-002 | KPI | KPI report for January | ❌ FAIL | None | 0.0 | 132.79 |
| KPI-003 | KPI | Lowest KPI projects | ❌ FAIL | None | 0.0 | 66.34 |
| PROP-001 | Proposal Analytics | Proposal win rate | ❌ FAIL | None | 0.0 | 71.4 |
| PROP-002 | Proposal Analytics | Proposal win rate for January | ❌ FAIL | None | 0.0 | 108.41 |
| REC-001 | Recoverability | Highest recoverability | ❌ FAIL | None | 0.0 | 110.97 |
| RCV-001 | Receivables | Outstanding receivables | ❌ FAIL | None | 0.0 | 58.18 |
| NAV-001 | Navigation | Executive Dashboard | ❌ FAIL | None | 0.0 | 64.31 |

## Failure Analysis
### REV-001 - Show revenue this month
- **Expected Capability**: revenue_analysis
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'revenue_analysis', got 'None'
  - Missing expected parameter: 'time_filter'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **37m45.408s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### REV-002 - Top 10 customers by revenue
- **Expected Capability**: analytical_query
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'analytical_query', got 'None'
  - Missing expected parameter: 'ranking'
  - Missing expected parameter: 'metric'
  - Missing expected parameter: 'entity'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **37m47.136s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### REV-003 - Compare FY2025 vs FY2024
- **Expected Capability**: analytical_query
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'analytical_query', got 'None'
  - Missing expected parameter: 'comparison'
  - Missing expected parameter: 'time_filter'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **37m48.864s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### KPI-001 - KPI report
- **Expected Capability**: kpi_summary
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'kpi_summary', got 'None'

### KPI-002 - KPI report for January
- **Expected Capability**: kpi_summary
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'kpi_summary', got 'None'
  - Missing expected parameter: 'time_filter'

### KPI-003 - Lowest KPI projects
- **Expected Capability**: analytical_query
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'analytical_query', got 'None'
  - Missing expected parameter: 'ranking'
  - Missing expected parameter: 'metric'
  - Missing expected parameter: 'entity'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **37m46.272s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### PROP-001 - Proposal win rate
- **Expected Capability**: analytical_query
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'analytical_query', got 'None'
  - Missing expected parameter: 'metric'
  - Missing expected parameter: 'entity'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **36m36.288s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### PROP-002 - Proposal win rate for January
- **Expected Capability**: analytical_query
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'analytical_query', got 'None'
  - Missing expected parameter: 'metric'
  - Missing expected parameter: 'entity'
  - Missing expected parameter: 'time_filter'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **37m45.408s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### REC-001 - Highest recoverability
- **Expected Capability**: analytical_query
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'analytical_query', got 'None'
  - Missing expected parameter: 'metric'
  - Missing expected parameter: 'ranking'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **35m16.8s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### RCV-001 - Outstanding receivables
- **Expected Capability**: receivables_analysis
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'receivables_analysis', got 'None'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **36m37.152s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._

### NAV-001 - Executive Dashboard
- **Expected Capability**: ui_navigation
- **Actual Capability**: None
- **Reasons for failure**:
  - Capability mismatch: expected 'ui_navigation', got 'None'
  - Missing expected parameter: 'target'
  - Unexpected clarification: ⏳ The AI service is temporarily at capacity. Please try again in **37m42.816s**.

_If this persists, the daily token quota may be exhausted — please contact your system administrator._
  - Navigation mismatch: expected 'executive_dashboard', got 'None'
