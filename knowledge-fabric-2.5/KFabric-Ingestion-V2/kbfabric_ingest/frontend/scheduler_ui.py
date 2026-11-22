"""
Pulse Knowledge Fabric - Ingestion Panel (Scheduler UI)

Schedule and monitor parallel document ingestion jobs.

Features:
- Submit jobs for multiple domains
- Monitor multiple jobs simultaneously  
- View progress from Ray Jobs
- Link to reports when complete
- Link to Ray Dashboard

Usage:
    streamlit run kbfabric_ingest/frontend/scheduler_ui.py --server.port 6003
"""

# CRITICAL: Disable OpenTelemetry BEFORE any imports to prevent browser hanging
import os
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["OTEL_ENABLED"] = "false"
os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"] = "*"

import time
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

# Configuration is loaded via env_config (repo-root ``env`` / ``env.test``).
from kbfabric_ingest._common.env_config import load_dotenv_file, require_env

load_dotenv_file()
API_URL = require_env("KBFABRIC_API_URL")
# Prefer public LAN URL for browser links; fall back to client URL.
RAY_DASHBOARD_URL = (os.environ.get("RAY_DASHBOARD_PUBLIC_URL") or "").strip() or require_env("RAY_DASHBOARD_URL")

# Enterprise theme (aligned with reports): white bg, black text, navy/magenta accents
COLORS = {
    "navy": "#0B1F3B",
    "magenta": "#B0006D",
    "success": "#137333",
    "warning": "#B26A00",
    "error": "#B42318",
    "bg": "#FFFFFF",
    "soft": "#F7F8FA",
    "border": "#D7DCE3",
    "text": "#111111",
    "muted": "#4B5563",
}

# Report link display names
LINK_NAMES = {
    "html": "Semantic",
    "json": "JSON",
    "ie_audit": "IE Audit",
    "kg_graph": "Graph"
}

# ============================================================
# Page Config - MUST be first Streamlit call
# ============================================================

st.set_page_config(
    page_title="Pulse - Ingestion Panel",
    page_icon="🔮",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ============================================================
# API Functions - Pure Python, no Streamlit calls
# ============================================================

def _forwarded_auth_headers() -> dict[str, str]:
    """Forward the authenticated gateway token from the current Streamlit request."""
    try:
        authorization = st.context.headers.get("Authorization")
    except Exception:
        authorization = None
    return {"Authorization": authorization} if authorization else {}

def api_get(endpoint: str, timeout: int = 2, params: dict | None = None):
    """GET request to API."""
    try:
        r = requests.get(
            f"{API_URL}{endpoint}", params=params,
            headers=_forwarded_auth_headers(), timeout=timeout,
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None

def api_post(endpoint: str, data: dict, timeout: int = 30):
    """POST request to API."""
    try:
        r = requests.post(
            f"{API_URL}{endpoint}", json=data,
            headers=_forwarded_auth_headers(), timeout=timeout,
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def get_health():
    """Get API health status."""
    return api_get("/health", timeout=2)

def get_ingestion_customers():
    """Get active customer choices for the interim workspace selector."""
    data = api_get("/ingestion/customers", timeout=5)
    return data.get("customers", []) if data else []

def get_ingestion_context(customer_id: str):
    """Get ontology-configured domains and product lines for one customer."""
    return api_get("/ingestion/context", timeout=5, params={"customer_id": customer_id})

def submit_job(customer_id: str, domain: str, product_line_id: str, input_dir: str,
               document_nature: str, hitl_mode: str):
    """Submit a new customer-scoped ingestion job."""
    payload = {
        "customer_id": customer_id,
        "domain": domain,
        "product_line_id": product_line_id,
        "document_nature": document_nature,
        "input_dir": input_dir,
        "force_rewrite": False,
        "hitl_mode": hitl_mode,
    }
    return api_post("/jobs", payload)

def get_jobs(limit: int = 20):
    """Get list of jobs."""
    data = api_get(f"/jobs?limit={limit}", timeout=2)
    return data.get("jobs", []) if data else []

def get_job_status(job_id: str):
    """Get detailed job status."""
    return api_get(f"/jobs/{job_id}", timeout=2)

def get_reports():
    """Get list of reports."""
    data = api_get("/reports", timeout=2)
    return data.get("reports", []) if data else []

def get_config_from_api():
    """Get configuration from API."""
    return api_get("/config", timeout=2)

# ============================================================
# Helper Functions
# ============================================================

def format_elapsed_time(seconds: float | None) -> str:
    """Format elapsed seconds into human-readable format (e.g., '2m 15s' or '1h 23m')."""
    if seconds is None:
        return "N/A"
    
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        if secs == 0:
            return f"{minutes}m"
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes == 0:
            return f"{hours}h"
        return f"{hours}h {minutes}m"

def format_number(num: int | None) -> str:
    """Format number with commas for readability."""
    if num is None:
        return "-"
    return f"{num:,}"

# ============================================================
# Main Application
# ============================================================

def main():
    """Main application with proper state management."""
    
    # Initialize session state
    if 'jobs' not in st.session_state:
        st.session_state.jobs = []
    if 'job_details_cache' not in st.session_state:
        st.session_state.job_details_cache = {}
    if 'last_refresh' not in st.session_state:
        st.session_state.last_refresh = None
    if 'api_health_checked' not in st.session_state:
        st.session_state.api_health_checked = False
    if 'api_health_status' not in st.session_state:
        st.session_state.api_health_status = None
    
    # Render CSS first
    st.markdown(f"""
    <style>
        .stApp {{ background: {COLORS['bg']}; color: {COLORS['text']}; }}
        
        /* Remove top spacing */
        [data-testid="stAppViewContainer"] {{ padding-top: 0; }}
        [data-testid="stHeader"] {{ display: none; }}
        
        .main-header {{
            background: {COLORS['bg']};
            color: {COLORS['navy']};
            padding: 1.2rem 1.5rem;
            padding-top: 0 !important;
            border-radius: 10px;
            margin-top: 0 !important;
            margin-bottom: 1.5rem;
            border: 1px solid {COLORS['border']};
        }}
        .main-header h1 {{ 
            margin: 0; 
            margin-top: 0 !important;
            padding-top: 0 !important;
            font-size: 1.5rem; 
            font-weight: 700;
        }}
        
        /* Remove top spacing from first element */
        [data-testid="stAppViewContainer"] > [data-testid="stBlockContainer"]:first-child {{
            margin-top: 0 !important;
            padding-top: 0 !important;
        }}
        .main-header h1 a {{ 
            text-decoration: none !important;
            color: inherit !important;
            pointer-events: none;
        }}
        .main-header .sub {{ color: {COLORS['muted']}; font-size: 0.9rem; }}
        
        .job-card {{
            background: white;
            border-radius: 10px;
            padding: 1rem;
            margin: 0.5rem 0;
            border-left: 4px solid {COLORS['navy']};
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
            border: 1px solid {COLORS['border']};
        }}
        .job-card.running {{ border-left-color: {COLORS['magenta']}; }}
        .job-card.succeeded {{ border-left-color: {COLORS['success']}; }}
        .job-card.failed {{ border-left-color: {COLORS['error']}; }}
        
        .status-badge {{
            display: inline-block;
            padding: 0.2rem 0.6rem;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 600;
        }}
        .status-running {{ background: {COLORS['magenta']}; color: white; }}
        .status-succeeded {{ background: {COLORS['success']}; color: white; }}
        .status-failed {{ background: {COLORS['error']}; color: white; }}
        .status-pending {{ background: #718096; color: white; }}
        
        .stButton > button {{
            background: {COLORS['navy']};
            color: white !important;
            border: 2px solid rgba(0, 0, 0, 0.1) !important;
            border-radius: 4px !important;
            font-weight: 600 !important;
            white-space: nowrap !important;
            height: auto !important;
            padding: 0.6rem 1.2rem !important;
            min-height: 2.5rem !important;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.15), 
                        0 2px 4px rgba(0, 0, 0, 0.1),
                        inset 0 1px 0 rgba(255, 255, 255, 0.2) !important;
            transition: all 0.2s ease !important;
            transform: translateY(0);
        }}
        .stButton > button:hover {{
            background: {COLORS['magenta']} !important;
            border-color: {COLORS['magenta']} !important;
            box-shadow: 0 6px 8px rgba(0, 0, 0, 0.2), 
                        0 3px 6px rgba(0, 0, 0, 0.15),
                        inset 0 1px 0 rgba(255, 255, 255, 0.3) !important;
            transform: translateY(-1px);
        }}
        .stButton > button:active {{
            transform: translateY(0);
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.15),
                        inset 0 2px 4px rgba(0, 0, 0, 0.2) !important;
        }}
        
        [data-testid="stMetricValue"] {{ color: {COLORS['navy']}; }}
        .stProgress > div > div > div {{ background: {COLORS['magenta']}; }}
        
        footer, #MainMenu, header {{ visibility: hidden; }}
        
        /* Sidebar resizing - best effort solution */
        [data-testid="stSidebar"] {{
            min-width: 250px;
            max-width: 400px;
        }}
        
        /* Reduce sidebar spacing */
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {{
            margin-top: 0 !important;
            margin-bottom: 0.1rem !important;
            padding-top: 0 !important;
            padding-bottom: 0.1rem !important;
        }}
        
        [data-testid="stSidebar"] .stMarkdown {{
            margin-bottom: 0.1rem !important;
        }}
    </style>
    """, unsafe_allow_html=True)
    
    # Render header
    st.markdown("""
    <div class="main-header">
        <h1>🔮 Pulse Knowledge Fabric</h1>
        <div class="sub">Ingestion Panel - Parallel Ingestion Jobs</div>
    </div>
    """, unsafe_allow_html=True)
    
    # Render tabs
    tab1, tab2, tab3 = st.tabs(["📤 Submit Job", "📋 Job Status", "📊 Reports"])
    
    # Sidebar - System Status
    with st.sidebar:
        st.markdown("### ⚙️ System Status")
        
        # Check API Status button - independent of job status
        check_api = st.button("🔄 Check API Status", key="check_api_btn")
        
        # Health status display - use empty container to avoid clearing jobs
        # Show status ABOVE auto-refresh checkbox
        health_status = st.empty()
        
        # Check API health ONLY when button is clicked (not on first load to avoid blocking)
        if check_api:
            try:
                # Use spinner to show we're checking, but don't block
                with st.spinner("Checking API..."):
                    health = get_health()
                    if health and health.get("status") in ("healthy", "degraded"):
                        health_status.success(f"✅ API Connected")
                        st.caption(f"Active jobs: {health.get('active_jobs', 0)}")
                        st.session_state.api_health_status = "connected"
                    else:
                        health_status.error("❌ API Unavailable")
                        st.caption("Start the Ingestion API first")
                        st.session_state.api_health_status = "unavailable"
            except Exception as e:
                health_status.error("❌ API Unavailable")
                st.caption("Start the Ingestion API first")
                st.session_state.api_health_status = "unavailable"
            st.session_state.api_health_checked = True
        else:
            # Show cached status or prompt user to check
            if st.session_state.api_health_status == "connected":
                health_status.success("✅ API Connected")
            elif st.session_state.api_health_status == "unavailable":
                health_status.error("❌ API Unavailable")
            else:
                # First load - don't check automatically, just show prompt
                health_status.info("⏳ Click 'Check API Status' to verify connection")
        
        # Auto-refresh checkbox - BELOW API status
        auto_refresh = st.checkbox("🔄 Auto-refresh (3s)", value=st.session_state.get('auto_refresh', False))
        st.session_state.auto_refresh = auto_refresh
        
        st.markdown(f"[🔗 Ray Dashboard]({RAY_DASHBOARD_URL})")
        st.markdown(f"[📖 API Docs]({API_URL}/docs)")
        st.markdown("---")
        
        # Configuration expander
        with st.expander("⚙️ Configuration", expanded=False):
            if st.session_state.api_health_checked and st.session_state.api_health_status == "connected":
                try:
                    config = get_config_from_api()
                    if config and not config.get("error"):
                        st.markdown("**LLM Connection**")
                        ollama = config.get("ollama", {})
                        st.caption(f"URL: `{ollama.get('base_url', 'unknown')}`")
                        st.caption(f"Text Model: `{ollama.get('text_model', 'unknown')}`")
                        st.caption(f"Vision Model: `{ollama.get('vision_model', 'unknown')}`")
                        st.caption(f"Timeout: `{ollama.get('timeout_sec', 300)}s`")
                        
                        st.markdown("**Batch Settings**")
                        ingestion = config.get("ingestion", {})
                        text_cfg = config.get("ollama", {}).get("text", {})
                        vision_cfg = config.get("ollama", {}).get("vision", {})
                        st.caption(f"Text Batch Size: `{text_cfg.get('batch_size', 8)}` blocks")
                        st.caption(f"Text Delay: `{text_cfg.get('batch_delay_seconds', 8.0)}s`")
                        st.caption(f"Vision Batch Size: `{vision_cfg.get('batch_size', 4)}` images")
                        st.caption(f"Vision Delay: `{vision_cfg.get('batch_delay_seconds', 10.0)}s`")
                        st.caption(f"Max Chars/Block: `{ingestion.get('max_chars_per_block', 1000)}`")
                        
                        st.markdown("**RAGAs Evaluation**")
                        ragas = config.get("ragas", {})
                        st.caption(f"Model: `{ragas.get('model', 'unknown')}`")
                        st.caption(f"Provider: `{ragas.get('provider', 'unknown')}`")
                    else:
                        st.caption("Configuration unavailable")
                except Exception:
                    st.caption("Configuration unavailable")
            else:
                st.caption("Check API status first to load configuration")
    
    # ==================== TAB 1: Submit Job ====================
    with tab1:
        st.markdown("### Submit New Ingestion Job")
        customers = get_ingestion_customers()
        if not customers:
            st.error("No active customer choices are available. Check API connectivity and customer setup.")
        else:
            customer = st.selectbox(
                "Customer workspace",
                customers,
                format_func=lambda item: f"{item['customer_name']} · {item['industry_id']}",
                key="ingestion_customer",
            )
            customer_id = customer["customer_id"]
            context = get_ingestion_context(customer_id)
            domains = context.get("domains", []) if context else []
            if not domains:
                st.error("This customer has no configured ingestion domains, or the context API is unavailable.")
            else:
                domain_option = st.selectbox(
                    "Domain",
                    domains,
                    format_func=lambda item: item.get("name", item.get("id", "")),
                    key="ingestion_domain",
                )
                product_lines = domain_option.get("product_lines", [])
                if not product_lines:
                    st.error("The selected domain has no applicable configured product lines.")
                else:
                    product_line = st.selectbox(
                        "Product line",
                        product_lines,
                        format_func=lambda item: item.get("name", item.get("id", "")),
                        key="ingestion_product_line",
                    )
                    col1, col2 = st.columns(2)
                    with col1:
                        input_dir = st.text_input("Input Directory", value="")
                        document_nature = st.selectbox(
                            "Document nature",
                            ["qe_assets", "technology_artifacts"],
                            format_func=lambda value: {
                                "qe_assets": "QE assets",
                                "technology_artifacts": "Technology artefacts",
                            }[value],
                        )
                    with col2:
                        hitl_mode = st.selectbox(
                            "Review policy",
                            ["review_ambiguous", "review_all", "auto_write"],
                            format_func=lambda value: {
                                "review_ambiguous": "Review ambiguous",
                                "review_all": "Review everything",
                                "auto_write": "Skip review",
                            }[value],
                        )
                        st.caption("Force rewrite is unavailable here because it clears shared domain data.")

                    if st.button("🚀 Submit Job", type="primary", use_container_width=True):
                        if input_dir:
                            with st.spinner("Submitting to Ray..."):
                                result = submit_job(
                                    customer_id,
                                    domain_option["id"],
                                    product_line["id"],
                                    input_dir,
                                    document_nature,
                                    hitl_mode,
                                )
                            if result.get("job_id"):
                                st.success(f"✅ Job submitted: `{result['job_id']}`")
                                st.info(f"[View in Ray Dashboard]({result.get('ray_dashboard_url', RAY_DASHBOARD_URL)})")
                                st.balloons()
                                st.session_state.jobs = []
                                st.session_state.last_refresh = None
                            else:
                                st.error(f"❌ Failed: {result.get('error', result.get('detail', 'Unknown error'))}")
                        else:
                            st.warning("Please enter an input directory")
    
    # ==================== TAB 2: Job Status ====================
    with tab2:
        st.markdown("### Active & Recent Jobs")
        
        col1, col2 = st.columns([1, 4])
        with col1:
            refresh_jobs = st.button("🔄 Refresh", key="refresh_jobs")
        
        # Determine if we need to refresh jobs
        should_refresh = (
            refresh_jobs or 
            not st.session_state.jobs or 
            auto_refresh or
            st.session_state.last_refresh is None
        )
        
        # Load jobs if needed
        if should_refresh:
            try:
                jobs = get_jobs(20)
                st.session_state.jobs = jobs
                st.session_state.last_refresh = time.time()
            except Exception as e:
                st.error(f"Could not load jobs: {e}")
                # Keep existing jobs on error
                jobs = st.session_state.jobs
        else:
            jobs = st.session_state.jobs
        
        # Display jobs
        if not jobs:
            st.info("No jobs found. Submit a new job from the first tab.")
        else:
            # Summary metrics
            running = sum(1 for j in jobs if j.get("status") == "RUNNING")
            pending = sum(1 for j in jobs if j.get("status") == "PENDING")
            succeeded = sum(1 for j in jobs if j.get("status") == "SUCCEEDED")
            failed = sum(1 for j in jobs if j.get("status") == "FAILED")
            
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("🔄 Running", running)
            m2.metric("⏳ Pending", pending)
            m3.metric("✅ Succeeded", succeeded)
            m4.metric("❌ Failed", failed)
            
            st.markdown("---")
            
            # Job cards
            for job in jobs:
                job_id = job.get("job_id", "unknown")
                status = (job.get("status") or "unknown").upper()
                domain = job.get("domain", "N/A")
                phase = job.get("phase", "")
                progress = job.get("progress_pct", 0) or 0
                
                status_emoji = {"RUNNING": "🔄", "SUCCEEDED": "✅", "FAILED": "❌", "PENDING": "⏳"}.get(status, "❓")
                
                # Fetch detailed status for RUNNING and SUCCEEDED jobs to get statistics
                # Explicitly scope details to this iteration to avoid closure issues
                current_job_id = job_id  # Capture job_id for this iteration
                details = job
                if status in ("RUNNING", "SUCCEEDED"):
                    if current_job_id not in st.session_state.job_details_cache:
                        # Fetch detailed status
                        detailed_status = get_job_status(current_job_id)
                        if detailed_status:
                            # Verify the detailed status matches the job_id
                            if detailed_status.get("job_id") == current_job_id:
                                st.session_state.job_details_cache[current_job_id] = detailed_status
                                details = detailed_status
                            else:
                                # Mismatch - don't cache, use job data
                                details = job
                        else:
                            details = job
                    else:
                        cached_details = st.session_state.job_details_cache[current_job_id]
                        # Verify cached details match current job_id
                        if cached_details.get("job_id") == current_job_id:
                            details = cached_details
                        else:
                            # Cache mismatch - refresh
                            detailed_status = get_job_status(current_job_id)
                            if detailed_status and detailed_status.get("job_id") == current_job_id:
                                st.session_state.job_details_cache[current_job_id] = detailed_status
                                details = detailed_status
                            else:
                                details = job
                
                with st.container():
                    col1, col2, col3 = st.columns([3, 2, 1])
                    
                    with col1:
                        # Use current_job_id consistently to avoid any scoping issues
                        st.markdown(f"**{status_emoji} {current_job_id}**")
                        st.caption(f"Domain: {domain}")
                        
                        started_at = details.get("started_at")
                        elapsed = details.get("elapsed_sec")
                        updated_at = details.get("updated_at")
                        phase_number = details.get("phase_number")
                        total_phases = details.get("total_phases")
                        warning_count = details.get("warning_count") or 0
                        
                        # Display time information with full date/time
                        if started_at:
                            try:
                                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                                time_str = f"Started: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                                if elapsed is not None:
                                    elapsed_str = format_elapsed_time(elapsed)
                                    time_str += f"  |  Elapsed: {elapsed_str}"
                                st.caption(time_str)
                            except:
                                if elapsed is not None:
                                    elapsed_str = format_elapsed_time(elapsed)
                                    st.caption(f"Elapsed: {elapsed_str}")
                        
                        # Display phase information
                        if phase_number and total_phases:
                            st.caption(f"Phase: {phase_number}/{total_phases}")
                        elif phase:
                            st.caption(f"Phase: {phase}")
                    
                    with col2:
                        if status == "RUNNING":
                            st.progress(progress / 100)
                            st.caption(f"{phase} ({progress}%)")
                        elif status == "SUCCEEDED":
                            warning_count = details.get("warning_count") or 0
                            if warning_count:
                                st.warning(f"Completed with warnings ({warning_count})")
                            else:
                                st.success("Completed")
                        elif status == "FAILED":
                            st.error(details.get("error", "Failed"))
                        else:
                            st.info(status)
                    
                    with col3:
                        st.markdown(f"[📊 Ray]({RAY_DASHBOARD_URL}/#/jobs/{current_job_id})")
                        
                        # Display report links for succeeded jobs
                        # CRITICAL: Verify details belong to current job before displaying reports
                        # Double-check to prevent wrong job's reports from being displayed
                        details_job_id = details.get("job_id")
                        if status == "SUCCEEDED" and details_job_id == current_job_id:
                            # Get report_paths and create a fresh dict to avoid reference issues
                            report_paths_raw = details.get("report_paths")
                            if report_paths_raw:
                                try:
                                    # Create a fresh copy of report_paths for this job
                                    report_paths = dict(report_paths_raw)  # Fresh copy
                                    
                                    # Verify we have report paths for this specific job
                                    for name, path in report_paths.items():
                                        # Ensure path is a string and extract relative path
                                        rel_path = str(path).replace("\\", "/")
                                        if "reports/" in rel_path:
                                            rel_path = rel_path.split("reports/", 1)[1]
                                        elif "reports\\" in rel_path:
                                            rel_path = rel_path.split("reports\\", 1)[1]
                                        
                                        display_name = LINK_NAMES.get(name, name)
                                        
                                        # Display link - each iteration creates a unique markdown element
                                        # The URL itself is unique per job, so Streamlit will render correctly
                                        st.markdown(f"[📄 {display_name}]({API_URL}/reports/{rel_path})")
                                except Exception as e:
                                    st.caption(f"Reports: unavailable (error: {str(e)[:50]})")
                        elif status == "SUCCEEDED":
                            # Job succeeded but details don't match or report_paths missing
                            if details_job_id != current_job_id:
                                # Job ID mismatch detected - don't show reports
                                st.caption(f"⚠️ Report data mismatch")
                            else:
                                st.caption("Reports: not available")
                    
                    # Display statistics section for RUNNING and SUCCEEDED jobs
                    if status in ("RUNNING", "SUCCEEDED"):
                        files_done = details.get("files_done")
                        files_total = details.get("files_total")
                        blocks_extracted = details.get("blocks_extracted")
                        entities_found = details.get("entities_found")
                        weaviate_chunks = details.get("weaviate_chunks")
                        neo4j_nodes = details.get("neo4j_nodes")
                        neo4j_relationships = details.get("neo4j_relationships")
                        
                        # Only show statistics if we have at least some data
                        if any([files_done is not None, files_total is not None, blocks_extracted is not None, 
                               entities_found is not None, weaviate_chunks is not None, 
                               neo4j_nodes is not None, neo4j_relationships is not None]):
                            st.markdown("**📊 Statistics:**")
                            
                            # Files progress
                            if files_done is not None and files_total is not None:
                                files_pct = int((files_done / files_total * 100)) if files_total > 0 else 0
                                st.markdown(f"   **Files:** {format_number(files_done)} / {format_number(files_total)} processed")
                                st.progress(files_pct / 100)
                            elif files_total is not None:
                                st.markdown(f"   **Files:** {format_number(files_total)} total")
                            
                            # Create columns for stats display
                            stat_col1, stat_col2 = st.columns(2)
                            
                            with stat_col1:
                                if blocks_extracted is not None:
                                    st.markdown(f"   **Blocks:** {format_number(blocks_extracted)} extracted")
                                if entities_found is not None:
                                    st.markdown(f"   **Entities:** {format_number(entities_found)} found")
                                if weaviate_chunks is not None:
                                    st.markdown(f"   **Weaviate:** {format_number(weaviate_chunks)} chunks written")
                            
                            with stat_col2:
                                if neo4j_nodes is not None or neo4j_relationships is not None:
                                    neo4j_str = ""
                                    if neo4j_nodes is not None:
                                        neo4j_str += f"{format_number(neo4j_nodes)} nodes"
                                    if neo4j_relationships is not None:
                                        if neo4j_str:
                                            neo4j_str += ", "
                                        neo4j_str += f"{format_number(neo4j_relationships)} relationships"
                                    if neo4j_str:
                                        st.markdown(f"   **Neo4j:** {neo4j_str}")
                            
                            # Warnings
                            if warning_count > 0:
                                st.caption(f"⚠️ Warnings: {warning_count}")
                    
                    st.markdown("---")
    
    # ==================== TAB 3: Reports ====================
    with tab3:
        st.markdown("### Generated Reports")
        
        refresh_reports = st.button("🔄 Refresh Reports", key="refresh_reports")
        
        # Load reports
        if refresh_reports or 'reports' not in st.session_state:
            try:
                reports = get_reports()
                st.session_state.reports = reports
            except Exception as e:
                st.error(f"Could not load reports: {e}")
                reports = st.session_state.get('reports', [])
        else:
            reports = st.session_state.get('reports', [])
        
        if not reports:
            st.info("No reports available. Complete an ingestion job to generate reports.")
        else:
            for report in reports:
                col1, col2, col3 = st.columns([4, 1, 1])
                with col1:
                    st.markdown(f"**{report['name']}**")
                with col2:
                    st.caption(f"{report.get('size_kb', 0)} KB")
                with col3:
                    st.markdown(f"[🔗 Open]({API_URL}/reports/{report['name']})")
    
    # Auto-refresh handling
    if auto_refresh:
        time.sleep(3)
        st.rerun()
    
    # Footer
    st.markdown(f"""
    <div style="background: white; border-radius: 10px; padding: 0.8rem; margin-top: 2rem; font-size: 0.8rem;">
        <strong>Pipeline:</strong> Parse → PII Mask → LLM Extraction → Embed → Store (Weaviate + Neo4j)
        &nbsp;|&nbsp;
        <a href="{RAY_DASHBOARD_URL}" target="_blank">Ray Dashboard</a>
        &nbsp;|&nbsp;
        <a href="{API_URL}/docs" target="_blank">API Docs</a>
    </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
