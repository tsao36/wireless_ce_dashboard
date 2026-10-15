""" Generate customer bugs TCCB statistics from JIRA """

from datetime import datetime, date
import os
import sys
import json
import logging
import argparse
import re
from dataclasses import dataclass, fields, is_dataclass, field
from xml.etree import ElementTree as ET
import pandas as pd
import urllib3
try:
    import snowflake.connector as snowflake_connector
except Exception:
    snowflake_connector = None
try:
    import psycopg2
except Exception:
    import psycopg as psycopg2
from jira.exceptions import JIRAError
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    from psycopg2.extras import execute_values
except Exception:
    def execute_values(cursor, sql: str, argslist, page_size: int = 100):
        if not argslist:
            return
        if "%s" not in sql:
            raise ValueError("execute_values fallback expects SQL containing a single %s placeholder")
        prefix, _ = sql.split("%s", 1)
        values_sql = prefix.rstrip() + "(" + ", ".join(["%s"] * len(argslist[0])) + ")"
        cursor.executemany(values_sql, argslist)

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


# pylint: disable=wrong-import-position
# internal includes inside the APIs submodule
sys.path.append(os.path.join(os.path.dirname(__file__), "APIs"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "APIs"))
# fallback: allow jira_api from parent folder
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
try:
    import jira_api
except Exception:
    class _FieldItem:
        def __init__(self, value: str):
            self.value = value

    class _FieldShim:
        PROJECT = _FieldItem("project")
        ISSUE_TYPE = _FieldItem("issuetype")

    class _JiraApiMissingShim:
        class Jira:
            def __init__(self, *args, **kwargs):
                raise ModuleNotFoundError(
                    "jira_api dependencies are unavailable. Install required JIRA stack to use JiraBug features."
                )

        Field = _FieldShim

        JIRAError = Exception

    jira_api = _JiraApiMissingShim()
import requests
import Sherlock

# pylint: enable=wrong-import-position

# suppress HTTP request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(format="%(asctime)s [%(levelname)s][%(name)s] %(message)s")
log = logging.getLogger("CUSTOMER_BUGS")


def _load_env() -> None:
    if load_dotenv is None:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip() or default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _get_ips_snowflake_config() -> dict[str, str]:
    _load_env()
    return {
        "user": _env("SNOWFLAKE_IPS_USER"),
        "password": _env("SNOWFLAKE_IPS_PASSWORD"),
        "role": _env("SNOWFLAKE_IPS_ROLE"),
        "account": _env("SNOWFLAKE_IPS_ACCOUNT"),
        "warehouse": _env("SNOWFLAKE_IPS_WAREHOUSE"),
        "database": _env("SNOWFLAKE_IPS_DATABASE"),
    }

# definitions
SIGHTING = "Open->Sighting"
OPEN = "Open"
IN_PROGRESS = "In Progress"
PENDING = "Pending"
JIRA_TAT = "JIRA TAT"
DB_NA = "NA"
OTHER_OEM_CUSTOMER = "Other - OEM"

_CUSTOMER_CANONICAL_MAP = {
    "SHANGHAI SIXUNITED INTELLIGENT TECHNOLOGY": "SIXUNITED",
    "GUANGDONG OPPO MOBILE TELECOMMUNICATIONSCORP. LTD.": "OPPO",
    "NEXSTGO COMPANY LIMITED": "Nexstgo",
    "ACCOUNT FOR DEACTIVATED CONTACTS": OTHER_OEM_CUSTOMER,
    "EDIMAX TECHNOLOGY CO., LTD": "Edimax",
    "EDIMAX TECHNOLOGY CO., LTD.": "Edimax",
    "JAGUAR LAND ROVER - END CUSTOMER": "JAGUAR LAND ROVER",
    "SHENZHEN BITLAND INFORMATION TECHNOLOGYCO., LTD.": "Bitland",
    "SHENZHEN EMDOOR ELECTRONIC TECHNOLOGY CO., LTD.": "Emdoor",
    "SHANGHAI WINGTECH ELECTRONICS TECHNOLOGYCO., LTD.": "Wingtech",
    "FOXCONN TECHNOLOGY CO., LTD.": "Foxconn",
    "ALLION TEST LABS, INC.": "Allion",
    "SHENZHEN IP3 CENTRY INTELLIGENT TECHNOLO": "IPS3 Tech",
}


def _canonicalize_customer_name(customer_name: object) -> str:
    text = str(customer_name or "").strip()
    if not text:
        return DB_NA
    if text.upper() == DB_NA:
        return DB_NA

    return _CUSTOMER_CANONICAL_MAP.get(text.upper(), text.upper())

# take the beginning of the next year
DB_FUTURE_DATE = datetime(year=date.today().year + 1, month=1, day=1)


def _null_db_date(val):
    """Return None for placeholder future dates so DB stores NULL instead of 2027."""
    if isinstance(val, datetime) and val != DB_FUTURE_DATE:
        return val
    return None

@dataclass
class RunOptions:
    # JIRA heavy features
    enable_jira_tat: bool = True
    enable_jira_comment_analysis: bool = False
    enable_jira_initial_component: bool = True
    enable_jira_duplicate_sw_check: bool = True

    # Logging / verbosity
    log_jira_tat_dict: bool = False  # stop noisy per-bug dict logs

    # Limits for testing (0 = no limit)
    limit_ips: int = 0
    limit_jira: int = 0

    # DB write
    enable_db_insert: bool = True
    db_recreate_table: bool = True
    db_auto_add_missing_columns: bool = False
    db_use_batch_insert: bool = False   # baseline keeps your current row-by-row behavior
    db_batch_page_size: int = 200


def _read_int(prompt: str, default: int) -> int:
    try:
        s = input(prompt).strip()
        return default if s == "" else int(s)
    except Exception:
        return default


def _read_yn(prompt: str, default: bool) -> bool:
    s = input(prompt).strip().lower()
    if s == "":
        return default
    if s in ("y", "yes", "1", "true", "t"):
        return True
    if s in ("n", "no", "0", "false", "f"):
        return False
    return default


def apply_run_option(choice: int) -> RunOptions:
    opt = RunOptions()

    if choice == 0:
        return opt

    if choice == 1:
        opt.log_jira_tat_dict = False
        return opt

    if choice == 2:
        opt.enable_jira_comment_analysis = False
        return opt

    if choice == 3:
        opt.enable_jira_tat = False
        opt.log_jira_tat_dict = False
        return opt

    if choice == 4:
        opt.enable_jira_comment_analysis = False
        opt.enable_jira_tat = False
        opt.log_jira_tat_dict = False
        return opt

    if choice == 5:
        opt.enable_jira_duplicate_sw_check = False
        return opt

    if choice == 6:
        opt.enable_jira_initial_component = False
        return opt

    if choice == 7:
        opt.limit_ips = _read_int("Limit IPS rows (e.g. 200): ", 200)
        opt.limit_jira = _read_int("Limit JIRA rows (e.g. 200): ", 200)
        opt.log_jira_tat_dict = False
        return opt

    if choice == 8:
        opt.enable_db_insert = False
        opt.log_jira_tat_dict = False
        return opt

    if choice == 9:
        opt.db_use_batch_insert = True
        return opt

    if choice == 10:
        # Custom
        opt.enable_jira_tat = _read_yn("Enable JIRA TAT calculation? [Y/n] ", True)
        opt.log_jira_tat_dict = _read_yn("Print per-bug TAT dict logs? [y/N] ", False)
        opt.enable_jira_comment_analysis = _read_yn("Enable comment analysis? [y/N] ", False)
        opt.enable_jira_initial_component = _read_yn("Enable initial component extraction? [Y/n] ", True)
        opt.enable_jira_duplicate_sw_check = _read_yn("Enable duplicate SW-change recursion check? [Y/n] ", True)
        opt.limit_ips = _read_int("Limit IPS rows (0 = no limit): ", 0)
        opt.limit_jira = _read_int("Limit JIRA rows (0 = no limit): ", 0)
        opt.enable_db_insert = _read_yn("Enable DB insert? [Y/n] ", True)
        opt.db_recreate_table = _read_yn("Recreate table each run? [Y/n] ", True)
        opt.db_use_batch_insert = _read_yn("Use batch insert (execute_values)? [Y/n] ", True)
        opt.db_batch_page_size = _read_int("Batch page_size (default 200): ", 200)
        return opt

    if choice == 11:
        # ✅ Option 11: FAST "fetch IPS+JIRA + merge + update Postgres"
        # Keep only what's needed to merge + write DB, skip expensive JIRA extras.
        opt.enable_jira_comment_analysis = False
        opt.enable_jira_tat = False
        opt.enable_jira_duplicate_sw_check = False
        opt.enable_jira_initial_component = False
        opt.log_jira_tat_dict = False

        opt.enable_db_insert = True
        opt.db_use_batch_insert = True
        opt.db_batch_page_size = 500  # optional: tune if needed

        opt.limit_ips = 0
        opt.limit_jira = 0
        return opt

    # unknown -> baseline
    return opt


def pick_run_options_menu() -> RunOptions:
    print("\n=== Speed Test Menu ===")
    print("0) Baseline (current behavior)")
    print("1) Stop per-bug JIRA TAT dict printing")
    print("2) Skip JIRA comment analysis (faster)")
    print("3) Skip JIRA TAT calculation (much faster)")
    print("4) Skip BOTH comment analysis + TAT")
    print("5) Skip duplicate SW-change recursion check")
    print("6) Skip initial component extraction (use final only)")
    print("7) Limit IPS/JIRA counts (quick smoke test)")
    print("8) DB insert OFF (only fetch/merge)")
    print("9) Enable Postgres batch insert (recommended)")
    print("10) Custom toggles")
    print("11) FAST: fetch+merge+Postgres update (skip heavy JIRA, batch insert)")

    choice = _read_int("Choose [0-11] (default 1): ", 1)
    return apply_run_option(choice)


@contextmanager
def stage_timer(label: str, agg: dict):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        agg[label] = agg.get(label, 0.0) + (time.perf_counter() - t0)


@dataclass
class IpsBugData:
    """
    struct for IPS bug data
    """

    ips_case_number: int = 0
    ips_url: str = DB_NA
    ips_status: str = DB_NA
    ips_sub_status: str = DB_NA
    ips_title: str = DB_NA
    ips_priority: str = DB_NA
    ips_created_date: datetime = DB_FUTURE_DATE
    ips_closure_status: str = DB_NA
    ips_env_details: str = DB_NA
    ips_found_in_build: str = DB_NA
    ips_hardware: str = DB_NA
    ips_platform: str = DB_NA
    ips_oem: str = DB_NA
    ips_odm: str = DB_NA
    ips_category: str = DB_NA
    ips_jira_promo_status: str = DB_NA
    ips_reporter_account_name: str = DB_NA
    ips_owner_name: str = DB_NA
    ips_owner_email: str = DB_NA
    ips_close_pending_date: datetime = DB_FUTURE_DATE
    ips_closed_date: datetime = DB_FUTURE_DATE
    ips_jira_id: str = DB_NA
    ips_reporter_account_geo: str = DB_NA
    ips_os: str = DB_NA
    ips_last_modified_date: datetime = DB_FUTURE_DATE


@dataclass
class JiraAnalysis:
    """
    JIRA extended analysis conclusions
    """

    num_of_comments_by_reporter: int = 0
    num_of_total_comments: int = 0
    avg_reporter_response_time_hour: int = 0
    did_reporter_reply_to_questions: bool = True


@dataclass
class JiraBugData:
    """
    struct for JIRA bug data
    """

    jira_id: str = DB_NA
    jira_title: str = DB_NA
    jira_summary: str = DB_NA
    jira_type: str = DB_NA
    jira_exposure: str = DB_NA
    jira_created_date: datetime = DB_FUTURE_DATE
    jira_closed_date: datetime = DB_FUTURE_DATE
    jira_implemented_date: datetime = DB_FUTURE_DATE
    jira_verify_date: datetime = DB_FUTURE_DATE
    jira_affected_version: str = DB_NA
    jira_initial_component: str = DB_NA
    jira_final_component: str = DB_NA
    jira_is_sw_change: bool = False
    jira_state_reason: str = DB_NA
    jira_status: str = DB_NA
    jira_platform: str = DB_NA
    jira_nic: str = DB_NA
    jira_os: str = DB_NA
    jira_assignee: str = DB_NA
    jira_reporter_name: str = DB_NA
    jira_reporter_email: str = DB_NA
    jira_tat_hours: int = 0
    jira_sighting_hours: int = 0
    jira_open_hours: int = 0
    jira_in_progress_hours: int = 0
    jira_pending_hours: int = 0
    jira_customer_name: str = DB_NA
    jira_url: str = DB_NA
    jira_analysis: JiraAnalysis = field(default_factory=JiraAnalysis)
    jira_found_by: str = DB_NA
    jira_team: str = DB_NA
    jira_external_assignee: str = DB_NA


@dataclass
class HsdBugData:
    """Struct for HSD bug data (optional)."""

    hsd_id: str = DB_NA
    hsd_promoted_id: str = DB_NA
    hsd_status_reason: str = DB_NA
    hsd_customer_detail: str = DB_NA
    hsd_owner: str = DB_NA
    hsd_title: str = DB_NA
    hsd_submitted_date: datetime = DB_FUTURE_DATE
    hsd_updated_date: datetime = DB_FUTURE_DATE
    hsd_platform: str = DB_NA


@dataclass
class MergedBugData:
    """
    merged struct of JIRA and IPS data
    """

    ips_data: IpsBugData = field(default_factory=IpsBugData)
    jira_data: JiraBugData = field(default_factory=JiraBugData)
    hsd_data: HsdBugData = field(default_factory=HsdBugData)
    bug_project: str = DB_NA
    # the time since IPS was filled till JIRA promotion
    ips_tat_till_jira_hours: int = 0
    is_ips_promoted_to_jira: bool = False
    customer: str = DB_NA
    reporter: str = DB_NA
    engineer: str = DB_NA
    customer_closed_date: datetime = DB_FUTURE_DATE


class IpsBug:
    """
    TODO: need to update this and add link to WIKI for snowflake
    class handling all IPS bugs
    IPS is Intel external tracking system accessible to customers
    caution: for this class to work, need to configure the DB on the server
    https://wiki.ith.intel.com/display/BIBigData/Impala+ODBC+Driver
    https://wiki.ith.intel.com/display/BIBigData/Impyla%3A+Python+Client+for+Impala
    AGS permissions:
    >>> "BI BIG DATA - HADOOP - END USERS - PRODUCTION - IAH IPS ANALYTICS -BUSINESS ANALYST"
    """

    def __init__(self, created_year):
        log.info("IPS CTOR is called")

        # IPS fields translator
        self.__ips_fields_map = {
            "ips_case_number": "CASE_NBR",
            "ips_url": "CASE_ID",
            "ips_status": "STATUS_TXT",
            "ips_sub_status": "SUB_STATUS_TXT",
            "ips_title": "SUBJECT_TXT",
            "ips_priority": "PRIORITY_TXT",
            "ips_created_date": "CASE_CREATED_DTM",
            "ips_closure_status": "CASE_CLOSURE_TXT",
            "ips_env_details": "ENV_DETAIL_DSC",
            "ips_category": "CORE_ISSUE_SUBCATEGORY_EXTERNAL_TXT",
            "ips_jira_promo_status": "PROMO_STATUS_TXT",
            "ips_reporter_account_name": "ACCOUNT_NM",
            "ips_owner_name": "CASE_OWNER_NM",
            "ips_owner_email": "CASE_OWNER_EMAIL_TXT",
            "ips_close_pending_date": "DATE_TIME_CLOSE_PENDING_DTM",
            "ips_closed_date": "CASE_CLOSURE_DTM",
            "ips_jira_id": "BACKEND_ID",
            "ips_reporter_account_geo": "ACCOUNT_SUB_GEOGRAPHIC_NM",
            "ips_last_modified_date": "LAST_MODIFIED_DTM",
        }

        # one of IPS fields is "Environment Details" which has additional data
        self.__ips_env_details_fields_map = {
            "ips_found_in_build": "Found In Build",
            "ips_hardware": "Hardware",
            "ips_platform": "Platforms",
            "ips_oem": "OEM",
            "ips_odm": "ODM",
            "ips_os": "Operating System",
        }

        # search for issues starting this year till now 'case_cre_dt'
        self.__created_year = created_year

        # the URL prefix for the IPS bugs, need to add /<case_id>/view to get the full link
        self.__ips_url = "https://intel.lightning.force.com/lightning/r/Case"

    def __del__(self):
        log.info("IPS DTOR is called")

    def __parse_env_details_table(self, ips_bug_data: IpsBugData) -> None:
        """
        fill specific fields in ips_bug_data from "Environment Details" variable
        this is a 2D table with specific key/val data
        if the desired key is not found, "NA" will be filled instead
        for DB space optimization, delete this field after use
        assumption: 'env_details' and 'case_number' fields are not empty
        """
        table_data = {}

        raw_env = ips_bug_data.ips_env_details
        if not isinstance(raw_env, str) or not raw_env:
            ips_bug_data.ips_env_details = DB_NA
            return

        # table has <b> and </b> which breaks the XML parser, remove them
        xml_table_str = raw_env.replace("<b>", "").replace("</b>", "")

        # parse table
        try:
            table_obj = ET.XML(xml_table_str)
        except ET.ParseError:
            # table doesn't exist or not in a valid XML format
            # skip the parse, some fields will be empty
            log.info("[%d] invalid environment data (corrupted or non existing xml)", ips_bug_data.ips_case_number)
            # delete the data
            ips_bug_data.ips_env_details = DB_NA
            return

        # generate key val from each row
        tbl_rows = iter(table_obj)
        for tbl_row in tbl_rows:
            values = [col.text for col in tbl_row]
            if len(values) < 2:
                log.warning("[%d] environment row malformed (len=%d)", ips_bug_data.ips_case_number, len(values))
                continue
            table_data[values[0]] = values[1]

        # fix missing keys
        for key, val in self.__ips_env_details_fields_map.items():
            new_val = DB_NA
            if val in table_data:
                new_val = table_data[val]
                # in case value is None set back to default
                if not new_val:
                    new_val = DB_NA
                elif ";" in new_val:
                    # sometimes there are multiple strings separated by ';'
                    # not sure if same values replicated or different one
                    # anyway, for our purpose, take the first one
                    match = re.findall(r"^([^;]+)", new_val)
                    if match:
                        new_val = match[0]

            setattr(ips_bug_data, key, new_val)

        # DB space optimization
        # the "env_details" field is relatively large, since contains lots of irrelevant info
        # in addition, it is in xml format having tables and consumes much space
        # all the relevant data was already extracted and by zeroing this field, we can save ~80% of DB size
        # why do we still set value here and totally do not delete it from the struct?
        # [1] it is not that simple to delete attribute from dataclass
        # [2] there are generic functions adding data to DB, based on the dataclass struct
        #     therefore, better to avoid some tricks here and not complicate the DB addition code
        ips_bug_data.ips_env_details = DB_NA

    def get_all_bugs(self) -> list:
        """
        returns list of dictionaries with IPS data, for all IPS found in the jira_issues
        every JIRA should have link to IPS, however, if it doesn't have, this bug will not appear in the returned list
        """
        # list of IPS cases
        ips_bug_list = []

        ips_cfg = _get_ips_snowflake_config()
        db_name = f"{ips_cfg['database']}.sales_support_premier_analysis"
        table_name = "fact_case"
        ips_team = "WCS"
        ips_limit_env = _env("IPS_ROW_LIMIT")
        ips_limit = int(ips_limit_env) if ips_limit_env.isdigit() else None

        # SECURITY NOTE: Credentials and proxy settings should be moved to environment variables
        # or secure configuration management (e.g., .env file, secrets manager)
        # Current implementation uses Sherlock module which should handle secure credential storage
        
        # proxy is mandatory (configured via .env)
        proxy_http = _env("IPS_HTTP_PROXY")
        proxy_https = _env("IPS_HTTPS_PROXY")
        proxy_no = _env("IPS_NO_PROXY", f"{ips_cfg['account']}.snowflakecomputing.com")
        if proxy_http:
            os.environ["HTTP_PROXY"] = proxy_http
        if proxy_https:
            os.environ["HTTPS_PROXY"] = proxy_https
        if proxy_no:
            os.environ["NO_PROXY"] = proxy_no

        # connect to DB
        # SECURITY NOTE: Ensure Sherlock module securely stores credentials
        # Consider using environment variables or AWS Secrets Manager instead
        db_engine = None
        cursor = None
        
        try:
            connect_kwargs = {
                "user": ips_cfg["user"],
                "password": ips_cfg["password"],
                "role": ips_cfg["role"],
                "account": ips_cfg["account"],
                "warehouse": ips_cfg["warehouse"],
                "database": ips_cfg["database"],
                "schema": f"{db_name}.{table_name}",
            }
            if _env_bool("SNOWFLAKE_OCSP_FAIL_OPEN"):
                connect_kwargs["ocsp_fail_open"] = True
                log.warning("SNOWFLAKE_OCSP_FAIL_OPEN enabled; OCSP checks will fail open.")

            if snowflake_connector is None:
                raise ModuleNotFoundError(
                    "Snowflake connector is unavailable. Install `snowflake-connector-python` "
                    "and remove the conflicting `snowflake` package if present."
                )

            db_engine = snowflake_connector.connect(**connect_kwargs)

            cursor = db_engine.cursor()

            ips_query = (
                f"SELECT * FROM {db_name}.{table_name} WHERE "
                f"case_created_dtm >= '{self.__created_year}-01-01'"
                f" AND case_created_dtm < '{int(self.__created_year)+1}-01-01'"
                f" AND assigned_queue_ss_one_dsc = '{ips_team}'"
            )
            if ips_limit:
                ips_query += f" LIMIT {ips_limit}"

            # run query
            log.info("IPS query: %s", ips_query)
            cursor.execute(ips_query)

            try:
                rows = pd.DataFrame(cursor.fetch_pandas_all())
            except Exception as exc:
                log.warning("Snowflake pandas fetch failed; falling back to fetchall: %s", exc)
                rows = pd.DataFrame(cursor.fetchall(), columns=[col[0] for col in cursor.description])
            log.info("IPS: [%d] bugs were found", len(rows))
            
        finally:
            # Ensure resources are cleaned up even if error occurs
            if cursor:
                try:
                    cursor.close()
                except Exception as e:
                    log.warning("Failed to close Snowflake cursor: %s", e)
            if db_engine:
                try:
                    db_engine.close()
                except Exception as e:
                    log.warning("Failed to close Snowflake connection: %s", e)

            # remove proxy otherwise JIRA will not work :)
            # Clean up even if query failed
            if "HTTP_PROXY" in os.environ:
                del os.environ["HTTP_PROXY"]
            if "HTTPS_PROXY" in os.environ:
                del os.environ["HTTPS_PROXY"]
            if "NO_PROXY" in os.environ:
                del os.environ["NO_PROXY"]

        # parse the data into list of IpsBugData
        for row in rows.itertuples():
            ips_bug_data = IpsBugData()

            # get all fields
            for key, val in self.__ips_fields_map.items():
                ips_val = getattr(row, val)

                # handling types
                # using annotations will be faster, but for some reason, pylint doesn't like it
                # ips_bug_data.__annotations__[key]
                key_type = next(f.type for f in fields(IpsBugData) if f.name == key)

                if key_type == str:
                    ips_val = ips_val if ips_val else DB_NA
                elif key_type == datetime:
                    try:
                        # try to parse the time, if fails, means that this is not a real time
                        # this is not an error, not all the time fields really have time
                        # (e.g close date of a bug which is not closed yet)
                        ips_val = datetime.fromisoformat(str(ips_val))
                    except ValueError:
                        ips_val = DB_FUTURE_DATE
                elif key_type == bool:
                    ips_val = bool(ips_val)
                elif key_type == int:
                    ips_val = int(ips_val)

                setattr(ips_bug_data, key, ips_val)

            # fill additional fields
            self.__parse_env_details_table(ips_bug_data)

            # fix the closed date - take the min between the closed date and closed pending
            # update both fields to avoid further confusion when making reports
            ips_bug_data.ips_closed_date = min(ips_bug_data.ips_closed_date, ips_bug_data.ips_close_pending_date)
            ips_bug_data.ips_close_pending_date = ips_bug_data.ips_closed_date

            # generate url
            # the actual URL doesn't appear in the parameters, however, can be generated by case_id
            # override this variable and construct the correct URL
            ips_bug_data.ips_url = f"{self.__ips_url}/{ips_bug_data.ips_url}/view"

            # add bug to list
            ips_bug_list.append(ips_bug_data)

        return ips_bug_list


class JiraBug(jira_api.Jira):
    """
    Class handling all JIRA bugs
    """

    def __init__(self, created_year, opt: RunOptions):
        if not Sherlock.Jira.server:
            raise RuntimeError("Set JIRA_CLOUD_SERVER before connecting to Jira; the retired Data Center default is disabled.")
        super().__init__(False)
        log.info("successfully connected to JIRA")
        self.__created_year = created_year
        self.__date_format = "%Y-%m-%dT%H:%M:%S.%f%z"

        self._opt = opt
        self._perf = defaultdict(float)
        self._counts = defaultdict(int)
        self.__team_field_id = self.__resolve_custom_field_id(("Team (migrated)", "Team"))
        if self.__team_field_id:
            log.info("JIRA team field resolved to %s", self.__team_field_id)
        else:
            log.warning("JIRA Team (migrated) field was not found; team-based filtering may fail")
        self.__external_assignee_field_id = self.__resolve_custom_field_id("External Assignee")
        if self.__external_assignee_field_id:
            log.info("JIRA custom field resolved: External Assignee -> %s", self.__external_assignee_field_id)
        else:
            log.warning("JIRA custom field 'External Assignee' was not found; jira_external_assignee will stay NA")

    def __resolve_custom_field_id(self, field_name: str | Sequence[str]) -> str:
        """Resolve a Jira custom field id by one or more display names."""
        field_names = [field_name] if isinstance(field_name, str) else list(field_name)
        normalized_names = [name.strip().casefold() for name in field_names if str(name).strip()]
        if not normalized_names:
            return ""
        try:
            entries = super().get_jira().fields()
            for name in normalized_names:
                for entry in entries:
                    if str(entry.get("name", "")).strip().casefold() == name:
                        field_id = str(entry.get("id", "")).strip()
                        if field_id:
                            return field_id
        except Exception as exc:
            log.warning("Failed to resolve Jira custom field(s) '%s': %s", ", ".join(field_names), exc)
        return ""

    def __team_jql_field(self) -> str:
        """Return a Cloud-safe JQL reference for the migrated Team field."""
        match = re.fullmatch(r"customfield_(\d+)", self.__team_field_id, re.IGNORECASE)
        if match:
            return f"cf[{match.group(1)}]"
        return '"Team (migrated)"'

    @staticmethod
    def __custom_field_to_text(field_value: object) -> str:
        """Convert Jira custom field payload to text safely."""
        if field_value is None:
            return DB_NA

        value_attr = getattr(field_value, "value", None)
        if isinstance(value_attr, str) and value_attr.strip():
            return value_attr.strip()

        display_attr = getattr(field_value, "displayName", None)
        if isinstance(display_attr, str) and display_attr.strip():
            return display_attr.strip()

        name_attr = getattr(field_value, "name", None)
        if isinstance(name_attr, str) and name_attr.strip():
            return name_attr.strip()

        if isinstance(field_value, str):
            text = field_value.strip()
            return text if text else DB_NA

        if isinstance(field_value, dict):
            for key in ("displayName", "value", "name", "accountId"):
                raw = field_value.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
            return DB_NA

        if isinstance(field_value, (list, tuple, set)):
            items = []
            for item in field_value:
                item_text = JiraBug.__custom_field_to_text(item)
                if item_text != DB_NA:
                    items.append(item_text)
            return ", ".join(items) if items else DB_NA

        return DB_NA

    @contextmanager
    def _timeit(self, key: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._perf[key] += (time.perf_counter() - t0)
            self._counts[key] += 1

    def _get_issue_with_retry(self, key: str, **kwargs):
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                return super().get_jira().issue(key, **kwargs)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt >= max_attempts:
                    raise
                wait = 2 ** (attempt - 1)
                log.warning("JIRA connection error fetching %s (attempt %d/%d): %s", key, attempt, max_attempts, exc)
                time.sleep(wait)
            except JIRAError as exc:
                status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
                if status in {502, 503, 504} and attempt < max_attempts:
                    wait = 2 ** (attempt - 1)
                    log.warning("JIRA server error %s fetching %s (attempt %d/%d)", status, key, attempt, max_attempts)
                    time.sleep(wait)
                    continue
                raise

    def __get_linked_bugs(self, bug: object) -> list:
        """
        returns list of linked bugs (duplicates) for a specific bug
        """
        bug_set_str = set()
        bug_list_obj = []

        for history in self._get_issue_with_retry(bug.key, expand="changelog").changelog.histories:
            for item in history.items:
                if item.field == "Link":
                    bug_set_str.add(item.to)

        # convert linked bugs to jira objects
        for bug_str in bug_set_str:
            try:
                bug_list_obj.append(self._get_issue_with_retry(bug_str))
            except JIRAError:
                # issue is not found in JIRA - probably was deleted
                log.error("%s is not found in JIRA", bug_str)

        return bug_list_obj

    def __is_bug_fixed_as_sw_change(self, bug: object, recursion_depth=0) -> bool:
        """
        returns True in case bug was closed as SW change (Fixed)
        recursive function, which goes to duplicated bugs if needed
        recursion_depth is meant to avoid infinite loop in case of cyclic bug pointing
        """
        # verify infinite loop
        recursion_depth += 1
        if recursion_depth > 4:
            # cyclic pointing
            log.warning("[%s] duplicate bugs having cyclic link", bug.key)
            return False

        # get the bug current status
        try:
            status = bug.fields.status.name
            state_reason_value = self.__extract_state_reason_value(bug)
        except AttributeError as err:
            log.warning("[%s] missing expected JIRA field while checking SW-change: %s", bug.key, err)
            return False

        if status in ["Closed", "Verify"] and state_reason_value:
            # check state reason
            if "Duplicate" in state_reason_value:
                # this is a duplicate bug - check his father
                dup_bug_list = self.__get_linked_bugs(bug)

                # sanity check
                if not dup_bug_list:
                    # should not happen
                    log.error("[%s] is marked as duplicate, however, no bugs were linked", bug.key)
                    # bug was surely not fixed
                    return False

                # go over all the duplicate bugs recursively
                # if one of them was fixed, we assume that this one (which duplicates it) is fixed
                for dup_bug in dup_bug_list:
                    if self.__is_bug_fixed_as_sw_change(dup_bug, recursion_depth):
                        return True

                # none of the dup bugs was fixed
                return False

            if "Fixed" in state_reason_value:
                # either "fixed" or "fixed by parent"
                return True

        # implemented can be only on SW change
        if status == "Implemented":
            return True

        return False

    @staticmethod
    def __get_final_component(bug: object) -> str:
        """
        returns the final component bug was closed on
        """
        if bug.fields.components:
            return bug.fields.components[0].name

        log.warning("final component is not found for JIRA: %s", bug.key)
        return DB_NA

    def __get_initial_component(self, bug: object) -> str:
        """
        returns the initial component bug was filed on
        this is extracted by the first transition of None -> component
        """
        for history in self._get_issue_with_retry(bug.key, expand="changelog").changelog.histories:
            for item in history.items:
                if item.field == "Component":
                    if item.fromString is None:
                        return item.toString
                    # if we have "from", this is the component
                    return item.fromString

        # no transition was found, meaning, the initial component equals to the final
        return self.__get_final_component(bug)

    def __get_jira_advanced_analysis(self, bug: object) -> JiraAnalysis:
        """
        performs inner analysis based on comments and fills the JiraAnalysis data
        """
        comments_db = {}
        last_comment_time = datetime.strptime(bug.fields.created, self.__date_format).replace(tzinfo=None)
        last_question_to_reporter_time = last_comment_time
        reporter_response_time_hour = []
        jira_analysis = JiraAnalysis()
        jira_analysis.avg_reporter_response_time_hour = -1

        # sanity check that reporter exists
        if not bug.fields.reporter:
            log.error("no reporter was found for %s", bug.key)
            return jira_analysis

        # generate list of possible keywords (try all combinations with name)
        reporter_name_keywords = [
            # rfridbur
            bug.fields.reporter.name,
            # roi.fridburg@intel.com
            bug.fields.reporter.emailAddress,
            # Roi
            bug.fields.reporter.displayName.split(",")[0],
            # Fridburg
            bug.fields.reporter.displayName.split(",")[-1],
        ]

        for comment in self._get_issue_with_retry(bug.key, expand="renderedFields").fields.comment.comments:
            # sanity check - comment has 'real' author, including
            # try to filter out faceless accounts, some don't have email
            if comment.author and comment.author.emailAddress:
                # get the comment time
                comment_time = datetime.strptime(comment.created, self.__date_format).replace(tzinfo=None)

                # if comment already exists, in means that it is an 'edit'
                # replace the orig comment by the new (edited) comment
                comments_db[comment.id] = {
                    "comment_time": comment_time,
                    "comment_author": comment.author.name,
                    "is_comment_by_reporter": (comment.author.name == bug.fields.reporter.name),
                    "is_question_to_reporter": False,
                }

                # look for questions to the reporter (not by reporter)
                if comment.author.name != bug.fields.reporter.name:
                    # this comment is not by reporter
                    # look if there is a question targeted to reporter
                    if "?" in comment.body:
                        # clean the comment body, remove all special chars
                        # e.g. roi- --> roi, roi: --> roi
                        comment_body_str = re.sub("[^a-z]+", " ", comment.body.lower())
                        # create unique list of words
                        # looking for exact keyword match, since sometimes reporter
                        # name can be part of a word, e.g. "he" --> "the" or "tu" --> "tune"
                        comment_body_list = set(comment_body_str.split(" "))
                        # remove empty string
                        if "" in comment_body_list:
                            comment_body_list.remove("")

                        for keyword in reporter_name_keywords:
                            # look if any keyword has exact match
                            if keyword.strip().lower() in comment_body_list:
                                # we have a question to the reporter
                                # update the comment DB
                                comment_data = comments_db[comment.id]
                                comment_data["is_question_to_reporter"] = True
                                break

        # count reporter comments
        jira_analysis.num_of_comments_by_reporter = len(
            [comment for comment in comments_db.values() if comment["is_comment_by_reporter"]]
        )

        jira_analysis.num_of_total_comments = len(comments_db)

        # calculate average reporter reply time
        was_answer_provided = True
        # assumption: comments are sorted by time, old to new
        for comment in comments_db.values():
            # check if this is a question
            if comment["is_question_to_reporter"]:
                # log the question time only if this is a new question,
                # meaning, answer was provided to the previous one
                # if this is another question, but previous one is still unanswered
                # keep the original time
                if was_answer_provided:
                    # new question, since answer was provided for the previous one
                    last_question_to_reporter_time = comment["comment_time"]
                    was_answer_provided = False

            # check if this is an answer
            elif not was_answer_provided and comment["is_comment_by_reporter"]:
                # this is a reply
                time_delta_sec = (comment["comment_time"] - last_question_to_reporter_time).total_seconds()
                reporter_response_time_hour.append(time_delta_sec / 3600)
                # reset question flag
                was_answer_provided = True

        # make sure there are answers ti questions
        if reporter_response_time_hour:
            jira_analysis.avg_reporter_response_time_hour = round(
                sum(reporter_response_time_hour) / len(reporter_response_time_hour)
            )
        else:
            # list is empty - in such case avg_response_time_hour will stay -1
            # when looking in PBI, need to ignore -1 in all queries later
            if not was_answer_provided:
                # there were no replies by reporter
                log.info(
                    "[%s] there were %d question(s) asked to reporter and no reply was provided",
                    bug.key,
                    len([comment for comment in comments_db.values() if comment["is_question_to_reporter"]]),
                )
                # reporter was asked direct questions and never replied
                jira_analysis.did_reporter_reply_to_questions = False

        return jira_analysis

    @staticmethod
    def __extract_state_reason_value(bug: object) -> str:
        """Return the raw state reason value from known custom fields."""
        candidate_fields = ("customfield_10218", "customfield_10208")
        for field_name in candidate_fields:
            field_value = getattr(bug.fields, field_name, None)
            if not field_value:
                # fall back to raw payload in case the field was not mapped onto PropertyHolder
                raw_fields = getattr(bug, "raw", {}).get("fields", {})
                field_value = raw_fields.get(field_name)
            if not field_value:
                continue

            # Option fields typically expose a .value attribute
            option_value = getattr(field_value, "value", None)
            if option_value:
                return str(option_value)

            # Handle primitive strings or list responses defensively
            if isinstance(field_value, str):
                return field_value
            if isinstance(field_value, (list, tuple)):
                for option in field_value:
                    opt_val = getattr(option, "value", None) or (option if isinstance(option, str) else None)
                    if opt_val:
                        return str(opt_val)

        return ""

    @staticmethod
    def __get_state_reason(bug: object) -> str:
        """
        returns the current state reason of the bug
        """
        status = bug.fields.status.name
        state_reason_value = JiraBug.__extract_state_reason_value(bug)

        if status in ["Closed", "Verify"] and state_reason_value:
            return state_reason_value

        if status == "Implemented":
            return "Fixed"

        return DB_NA

    @staticmethod
    def __get_issue_type(bug: object) -> str:
        """
        returns the issue type (e.g. Bug, Task)
        """
        try:
            issue_type = getattr(getattr(bug.fields, "issuetype", None), "name", None)
            if issue_type:
                return str(issue_type)
        except (TypeError, AttributeError):
            return DB_NA
        return DB_NA

    @staticmethod
    def __get_status(bug: object) -> str:
        """
        returns the current status of the bug
        """
        return bug.fields.status.name

    @staticmethod
    def __get_platform(bug: object) -> str:
        """
        returns the platform bug was filed on
        the "platform" field is a list, need to choose the recent one
        """
        # using some hack: there is no easy way to know which platform is newer
        # e.g. MTL is newer than RPL, but how can I know that? need to go to other DBs
        # this is an overkill, therefore, use the JIRA field ID
        # it is assumed that these numbers are monotonically rising, therefore,
        # if number is higher, it means that platform was added later, meaning, it is newer
        # yes, not perfect, but for customer issues, we do not expect a single bug to
        # be filed on multiple platforms. this can happen for validation, but not for customers
        platform_id = 0
        platform_name = None

        try:
            for platform in bug.fields.customfield_10242:
                if int(platform.id) > platform_id:
                    platform_id = int(platform.id)
                    platform_name = platform.value
        except (TypeError, AttributeError):
            return DB_NA

        return platform_name

    @staticmethod
    def __get_hardware(bug: object) -> str:
        """
        returns the hardware (NIC) bug was filed on
        the "hardware" field is a list, need to choose the recent one
        """
        # using same hack as in __get_platform()
        hardware_id = 0
        hardware_name = None

        try:
            for hardware in bug.fields.customfield_10223:
                if int(hardware.id) > hardware_id:
                    hardware_id = int(hardware.id)
                    hardware_name = hardware.value
        except (TypeError, AttributeError):
            return DB_NA

        return hardware_name

    @staticmethod
    def __get_os(bug: object) -> str:
        """
        returns the OS bug was filed on
        the "os" field is a list, need to choose the recent one
        """
        # using same hack as in __get_platform()
        os_id = 0
        os_name = None

        try:
            for operating_system in bug.fields.customfield_10277:
                if int(operating_system.id) > os_id:
                    os_id = int(operating_system.id)
                    os_name = operating_system.value
        except (TypeError, AttributeError):
            return DB_NA

        return os_name

    @staticmethod
    def __get_exposure(bug: object) -> str:
        """
        returns the bug exposure
        """
        exposure = getattr(getattr(bug, "fields", None), "customfield_10252", None)

        if exposure is None:
            return DB_NA

        if hasattr(exposure, "value"):
            return exposure.value if exposure.value else DB_NA

        if isinstance(exposure, list):
            for item in exposure:
                if hasattr(item, "value") and item.value:
                    return item.value
                if isinstance(item, str) and item:
                    return item
            return DB_NA

        if isinstance(exposure, str):
            return exposure if exposure else DB_NA

        return DB_NA

    @staticmethod
    def __get_title(bug: object) -> str:
        """
        returns the bug subject/title
        """
        return bug.fields.summary

    @staticmethod
    def __get_reporter_name(bug: object) -> str:
        """
        returns the bug reporter
        """
        if bug.fields.reporter:
            return bug.fields.reporter.displayName

        # try creator if reporter is not found
        if bug.fields.creator:
            return bug.fields.creator.displayName

        log.warning("reporter is not found for JIRA: %s", bug.key)
        return DB_NA

    @staticmethod
    def __get_reporter_email(bug: object) -> str:
        """
        returns the bug reporter
        """
        if bug.fields.reporter:
            return bug.fields.reporter.emailAddress

        # try creator if reporter is not found
        if bug.fields.creator:
            return bug.fields.creator.emailAddress

        return DB_NA

    @staticmethod
    def __get_assignee_name(bug: object) -> str:
        """
        returns the bug assignee display name
        """
        assignee = getattr(bug.fields, "assignee", None)
        if not assignee:
            return DB_NA

        display = getattr(assignee, "displayName", None)
        if display:
            normalized = normalize_jira_assignee(display)
            return normalized if normalized != DB_NA else display

        name = getattr(assignee, "name", None)
        if name:
            normalized = normalize_jira_assignee(name)
            return normalized if normalized != DB_NA else name

        account_id = getattr(assignee, "accountId", None)
        if account_id:
            normalized = normalize_jira_assignee(account_id)
            return normalized if normalized != DB_NA else account_id

        return DB_NA

    @staticmethod
    def __get_customer_name(bug: object) -> str:
        """
        returns the customer name
        """
        try:
            return bug.fields.customfield_10207.value
        except (TypeError, AttributeError):
            return DB_NA

    def __get_url(self, bug: object) -> str:
        """
        returns the JIRA bug URL
        """
        return f"{Sherlock.Jira.server.rstrip('/')}/browse/{bug.key}"

    def __get_created_date(self, bug: object) -> datetime:
        """
        returns the bug created date
        """
        # '2022-07-06T09:47:50.000+0300'
        # remove the TZ
        return datetime.strptime(bug.fields.created, self.__date_format).replace(tzinfo=None)

    def __get_closed_date(self, bug: object) -> datetime:
        """
        returns the bug closed date
        """
        # '2022-07-06T09:47:50.000+0300'
        # remove the TZ
        closed_date = getattr(bug.fields, "customfield_10253", None)
        if closed_date:
            return datetime.strptime(closed_date, self.__date_format).replace(tzinfo=None)

        # if bug is not closed, we don't have any date
        # set something in future, since we still need datetime variable
        return DB_FUTURE_DATE

    def __get_implemented_date(self, bug: object) -> datetime:
        """
        returns the bug implemented date (if exists)
        """
        # '2022-07-06T09:47:50.000+0300'
        # remove the TZ
        implemented_date = getattr(bug.fields, "customfield_10575", None)
        if implemented_date:
            return datetime.strptime(implemented_date, self.__date_format).replace(tzinfo=None)

        # if bug is not implemented, we don't have any date
        # set something in future, since we still need datetime variable
        return DB_FUTURE_DATE

    def __get_verify_date(self, bug: object) -> datetime:
        """
        returns the bug verify date (if exists)
        """
        # '2022-07-06T09:47:50.000+0300'
        # remove the TZ
        if bug.fields.resolutiondate:
            return datetime.strptime(bug.fields.resolutiondate, self.__date_format).replace(tzinfo=None)

        # if bug is not moved to verify, we don't have any date
        # set something in future, since we still need datetime variable
        return DB_FUTURE_DATE

    @staticmethod
    def __get_affected_version(bug: object) -> str:
        """
        returns the affected version (cleaned - numbers only)
        in case of several, take the first one
        """
        if bug.fields.versions:
            ver = bug.fields.versions[0].name
            # version can sometimes be with REL/rel prefix and sometimes can have other format (free text)
            # try to spot something looking like a.b or a.b.c or a.b.c.d
            # need to align all to be a.b.c
            # 22.220.0.1
            if match := re.findall(r"(\d+\.\d+\.\d+)\.\d+", ver):
                # take first 3 bytes (22.220.0)
                return match[0]

            # 22.220.0
            if match := re.findall(r"\d+\.\d+\.\d+", ver):
                # take as is (22.220.0)
                return match[0]

            # 22.220
            if match := re.findall(r"\d+\.\d+", ver):
                # add 0 in 3rd byte (22.220.0)
                return f"{match[0]}.0"

        return DB_NA

    @staticmethod
    def __convert_time_str_to_object(time: str) -> datetime:
        """
        convert JIRA time format to datetime object
        """
        # '2022-09-05T20:11:46.093+0300'
        # remove everything after '.' --> '2022-09-05T20:11:46'
        time_without_ms = re.sub(r"\..*", "", time)
        datetime_format = "%Y-%m-%dT%H:%M:%S"
        return datetime.strptime(time_without_ms, datetime_format)

    @staticmethod
    def __calc_time_diff_hours(last_time: datetime, first_time: datetime) -> int:
        """
        calculate the time difference between last_time (new) and first_time (old)
        time diff is returned in hours, rounded to int
        in case any of the times is None, 0 will be returned
        """
        if last_time and first_time:
            return round((last_time - first_time).total_seconds() / 3600)

        # one of the dates is not available, this is not necessarily a bug
        return 0

    def __subtract_sighting_from_actual_tat(self, actual_dict: dict, sighting_dict: dict) -> None:
        """
        subtract all sighting states from the actual and update the actual_dict
        """
        for state in sighting_dict:
            if state in actual_dict:
                actual_dict[state] = actual_dict[state] - sighting_dict[state]

    def __calculate_tat(self, bug: object) -> dict:
        """
        calculate the overall turn-around time (TAT) and inner state distribution,
        meaning, how much time bug was in every state
        returns dict in the following format where all the values are in hours
        >>> {
                "state1": val1,
                "state2": val2
        >>> }
        """
        # desired status list will store the commutative time (hours) which bug spent in every state
        # 'sighting' is not a real state, will need to have some kombina to calculate it
        actual_status_duration_hours_dict = {}
        sighting_status_duration_hours_dict = {}
        status_duration_hours_dict = actual_status_duration_hours_dict

        # get the created date - this is simple
        jira_created_date = self.__convert_time_str_to_object(bug.fields.created)
        # get close date - this is a bit more tricky, since it is possible that bug was never implemented
        # (e.g. duplicate bugs) need to monitor all transitions and looking for specific states
        jira_closed_date = None

        # the reference time is the previous transition - init with bug creation date
        previous_transition_date = jira_created_date

        # log.info(
        #     "[%s] [%s] [%s] '*' --> Open",
        #     bug.key,
        #     str(bug.fields.reporter.displayName).ljust(30),
        #     jira_created_date,
        # )

        # Fetch changelog with retry to avoid transient network failures
        histories = None
        for attempt in range(1, 4):
            try:
                histories = super().get_jira().issue(bug.key, expand="changelog").changelog.histories
                break
            except (requests.exceptions.ConnectionError, urllib3.exceptions.ProtocolError) as exc:
                log.warning(
                    "JIRA changelog fetch failed for %s (attempt %d/3): %s",
                    bug.key,
                    attempt,
                    exc,
                )
                time.sleep(2 * attempt)
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("JIRA changelog fetch failed for %s: %s", bug.key, exc)
                break

        if not histories:
            log.warning("Skipping TAT calc for %s due to missing changelog", bug.key)
            return {}

        for history in histories:
            for item in history.items:
                # get the from/to states
                from_state = item.fromString
                to_state = item.toString
                # log the transition date
                current_transition_date = self.__convert_time_str_to_object(history.created)

                # 'sighting' is not a status, but a 'state'
                # you can be in sighting in any status
                if item.field == "State Reason":
                    if to_state == SIGHTING:
                        # entering sighting mode
                        status_duration_hours_dict = sighting_status_duration_hours_dict
                        log.debug(
                            "[%s] [%s] [%s] %s --> %s",
                            bug.key,
                            str(history.author.displayName).ljust(30),
                            current_transition_date,
                            from_state,
                            SIGHTING,
                        )

                    if from_state == SIGHTING:
                        # exit sighting mode
                        if not sighting_status_duration_hours_dict:
                            # the 'sighting' dict is empty. we never entered this state --> bug was filed as 'sighting'
                            # update dictionary with "Open" status (which is the default)
                            delta_hour = self.__calc_time_diff_hours(current_transition_date, previous_transition_date)
                            sighting_status_duration_hours_dict["Open"] = delta_hour

                        status_duration_hours_dict = actual_status_duration_hours_dict
                        log.debug(
                            "[%s] [%s] [%s] %s --> %s [%s]",
                            bug.key,
                            str(history.author.displayName).ljust(30),
                            current_transition_date,
                            SIGHTING,
                            to_state,
                            delta_hour,
                        )

                    # go to next transition, this should not change anything, besides turning on/off sighting flag
                    continue

                if item.field == "status":
                    if from_state not in status_duration_hours_dict:
                        status_duration_hours_dict[from_state] = 0

                    orig = int(status_duration_hours_dict[from_state])
                    delta_hour = self.__calc_time_diff_hours(current_transition_date, previous_transition_date)
                    status_duration_hours_dict[from_state] = orig + delta_hour
                    log.debug(
                        "[%s] [%s] [%s] %s --> %s [%s]",
                        bug.key,
                        str(history.author.displayName).ljust(30),
                        current_transition_date,
                        from_state,
                        to_state,
                        delta_hour,
                    )

                    # update closed date - any of the following sates is defined to be "closed"
                    # (take the first one, since transitions are in order)
                    if not jira_closed_date:
                        if to_state in ["Implemented", "Verify", "Closed"]:
                            jira_closed_date = current_transition_date

                    # store the previous transition date for later use
                    previous_transition_date = current_transition_date
                    continue

        # subtract the sighting time from the actual one
        self.__subtract_sighting_from_actual_tat(actual_status_duration_hours_dict, sighting_status_duration_hours_dict)

        # prepare final dict - add overall TAT and sighting
        actual_status_duration_hours_dict[JIRA_TAT] = self.__calc_time_diff_hours(jira_closed_date, jira_created_date)
        actual_status_duration_hours_dict[SIGHTING] = sum(sighting_status_duration_hours_dict.values())
        if self._opt.log_jira_tat_dict:
            log.info(actual_status_duration_hours_dict)


        return actual_status_duration_hours_dict

    def get_specific_bug(self, bug_number: str) -> str:
        """
        gets bug number eg. WIFI-1234 or WOT-567 and returns the corresponding bug ID in JIRA
        do you think it should be the same?
        """
        _actual_bug = DB_NA

        try:
            issue = self._get_issue_with_retry(bug_number)
            _actual_bug = issue.key
        except jira_api.JIRAError:
            # bug was not found
            pass
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            log.warning("Transient JIRA network error while resolving %s; keep DB_NA. error=%s", bug_number, exc)

        return _actual_bug

    @staticmethod
    def __get_found_by(bug: object) -> str:
        """
        returns the "found by" field
        """
        try:
            return bug.fields.customfield_10224.value
        except (TypeError, AttributeError):
            return DB_NA

    def __get_team(self, bug: object) -> str:
        """
        returns the "team" field
        """
        field_id = self.__team_field_id
        if not field_id:
            return DB_NA
        fields = getattr(bug, "fields", None)
        field_value = getattr(fields, field_id, None)
        if field_value is None:
            raw_payload = getattr(bug, "raw", None)
            raw_fields = raw_payload.get("fields", {}) if isinstance(raw_payload, dict) else {}
            field_value = raw_fields.get(field_id)
        return self.__custom_field_to_text(field_value)

    def __get_external_assignee(self, bug: object) -> str:
        """Return Jira External Assignee. This field is expected on GoogleDB sightings only."""
        team_value = self.__get_team(bug)
        if str(team_value).strip().lower() != "googledb":
            return DB_NA

        field_id = self.__external_assignee_field_id
        if not field_id:
            return DB_NA

        raw_value = getattr(bug.fields, field_id, None)
        if raw_value is None:
            raw_value = getattr(bug, "raw", {}).get("fields", {}).get(field_id)

        return self.__custom_field_to_text(raw_value)

    def __build_jira_bug_data(self, bug: object) -> JiraBugData:
        jira_bug_data = JiraBugData()

        jira_bug_data.jira_id = bug.key
        jira_bug_data.jira_title = self.__get_title(bug)
        jira_bug_data.jira_summary = self.__get_title(bug)
        jira_bug_data.jira_type = self.__get_issue_type(bug)
        jira_bug_data.jira_exposure = self.__get_exposure(bug)
        jira_bug_data.jira_created_date = self.__get_created_date(bug)
        jira_bug_data.jira_closed_date = self.__get_closed_date(bug)
        jira_bug_data.jira_implemented_date = self.__get_implemented_date(bug)
        jira_bug_data.jira_verify_date = self.__get_verify_date(bug)
        jira_bug_data.jira_affected_version = self.__get_affected_version(bug)

        jira_bug_data.jira_final_component = self.__get_final_component(bug)

        if self._opt.enable_jira_initial_component:
            with self._timeit("jira_initial_component"):
                jira_bug_data.jira_initial_component = self.__get_initial_component(bug)
        else:
            jira_bug_data.jira_initial_component = jira_bug_data.jira_final_component

        if self._opt.enable_jira_duplicate_sw_check:
            with self._timeit("jira_sw_change_check"):
                jira_bug_data.jira_is_sw_change = self.__is_bug_fixed_as_sw_change(bug)
        else:
            jira_bug_data.jira_is_sw_change = False

        jira_bug_data.jira_state_reason = self.__get_state_reason(bug)
        jira_bug_data.jira_status = self.__get_status(bug)
        jira_bug_data.jira_platform = self.__get_platform(bug)
        jira_bug_data.jira_nic = self.__get_hardware(bug)
        jira_bug_data.jira_os = self.__get_os(bug)
        jira_bug_data.jira_assignee = self.__get_assignee_name(bug)
        jira_bug_data.jira_reporter_name = self.__get_reporter_name(bug)
        jira_bug_data.jira_reporter_email = self.__get_reporter_email(bug)
        jira_bug_data.jira_customer_name = self.__get_customer_name(bug)
        jira_bug_data.jira_url = self.__get_url(bug)
        jira_bug_data.jira_found_by = self.__get_found_by(bug)
        jira_bug_data.jira_team = self.__get_team(bug)
        jira_bug_data.jira_external_assignee = self.__get_external_assignee(bug)

        if self._opt.enable_jira_comment_analysis:
            with self._timeit("jira_comment_analysis"):
                jira_bug_data.jira_analysis = self.__get_jira_advanced_analysis(bug)
        else:
            jira_bug_data.jira_analysis = JiraAnalysis()

        if self._opt.enable_jira_tat:
            with self._timeit("jira_tat_calc"):
                tat_values_hour = self.__calculate_tat(bug)

            jira_bug_data.jira_tat_hours = tat_values_hour.get(JIRA_TAT, 0)
            jira_bug_data.jira_sighting_hours = tat_values_hour.get(SIGHTING, 0)
            jira_bug_data.jira_open_hours = tat_values_hour.get(OPEN, 0)
            jira_bug_data.jira_in_progress_hours = tat_values_hour.get(IN_PROGRESS, 0)
            jira_bug_data.jira_pending_hours = tat_values_hour.get(PENDING, 0)

        return jira_bug_data

    def get_specific_bug_data(self, bug_number: str) -> Optional[JiraBugData]:
        """Fetch one Jira issue by key and convert it with the normal Jira field mapping."""
        try:
            issue = self._get_issue_with_retry(bug_number)
        except jira_api.JIRAError:
            return None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            log.warning("Transient JIRA network error while fetching %s; skipping targeted refresh. error=%s", bug_number, exc)
            return None

        return self.__build_jira_bug_data(issue)

    def get_all_bugs(self) -> list:
        """
        returns all JIRA bugs matching specific query
        """
        # lits for all jira bugs
        jira_bug_list = []

        def _canonical_team_name(raw_name: object) -> str:
            normalized = normalize_jira_assignee(raw_name)
            if normalized in (None, "", DB_NA):
                return ""
            return str(normalized).strip().lower()

        def _team_match_sources(issue: object) -> tuple[bool, bool, bool]:
            assignee_key = _canonical_team_name(self.__get_assignee_name(issue))
            reporter_key = _canonical_team_name(self.__get_reporter_name(issue))
            external_assignee_key = _canonical_team_name(self.__get_external_assignee(issue))

            matched_assignee = bool(assignee_key and assignee_key in _ENGINEER_NAME_CANONICAL_MAP)
            matched_reporter = bool(reporter_key and reporter_key in _ENGINEER_NAME_CANONICAL_MAP)
            matched_external = bool(external_assignee_key and external_assignee_key in _ENGINEER_NAME_CANONICAL_MAP)
            return matched_assignee, matched_reporter, matched_external

        team_field = self.__team_jql_field()
        base_team_filter = f"{team_field} in (CAE, 'CAE - Enterprise', 'CIE Engineering', 'CAE – Certifications', 'CAE - Linux')"
        # Always include Validation India Core Regression + 3rd Party->AP issues in the JIRA pull.
        extra_filter = f"{team_field} = 'Validation India -> Core Regression' and component = '3rd Party->AP'"
        # Also include GoogleDB team issues regardless of base CAE/CIE team filter.
        google_db_filter = f"{team_field} = 'GoogleDB'"
        team_filter = f"({base_team_filter} OR ({extra_filter}) OR ({google_db_filter}))"

        jira_query = (
            f"{jira_api.Field.PROJECT.value} in (WIFI, BT, CIE, DBGT, WOT) "
            f"and {jira_api.Field.ISSUE_TYPE.value} in (Bug, Task) "
            f"and statusCategory != Done "
            # restrict TEAM to valid options; drop invalid CAE-Linux spellings to avoid JIRA 400 errors
            f"and {team_filter} "
            f"and 'Created' >= {self.__created_year}-01-01"
            f" and 'Created' < {int(self.__created_year)+1}-01-01"
        )

        log.info("JIRA query: %s", jira_query)
        try:
            issues = super().get_jira().search_issues(jira_query, maxResults=False)
        except JIRAError as exc:
            # If JIRA rejects TEAM options (e.g., removed values), fall back to the confirmed "CAE" option
            msg = getattr(exc, "text", "") or str(exc)
            if "option" in msg.lower() and "does not exist" in msg.lower():
                fallback_team_filter = f"{team_field} = 'CAE'"
                fallback_team_filter = f"({fallback_team_filter} OR ({extra_filter}) OR ({google_db_filter}))"
                fallback_query = (
                    f"{jira_api.Field.PROJECT.value} in (WIFI, BT, CIE, DBGT, WOT) "
                    f"and {jira_api.Field.ISSUE_TYPE.value} in (Bug, Task) "
                    f"and statusCategory != Done "
                    f"and {fallback_team_filter} "
                    f"and 'Created' >= {self.__created_year}-01-01"
                    f" and 'Created' < {int(self.__created_year)+1}-01-01"
                )
                log.warning("JIRA TEAM option invalid; retrying with CAE-only. Error: %s", msg)
                log.info("JIRA fallback query: %s", fallback_query)
                issues = super().get_jira().search_issues(fallback_query, maxResults=False)
            else:
                raise

        total_before_member_filter = len(issues)
        matched_by_assignee = 0
        matched_by_reporter = 0
        matched_by_external_assignee = 0
        filtered_issues = []
        for issue in issues:
            is_assignee, is_reporter, is_external = _team_match_sources(issue)
            if is_assignee or is_reporter or is_external:
                filtered_issues.append(issue)
                matched_by_assignee += int(is_assignee)
                matched_by_reporter += int(is_reporter)
                matched_by_external_assignee += int(is_external)

        issues = filtered_issues
        dropped_member_count = total_before_member_filter - len(issues)
        if dropped_member_count:
            log.info(
                "JIRA team-member filter: dropped %d issues (no assignee/reporter/external_assignee in team list)",
                dropped_member_count,
            )
        log.info(
            "JIRA team-member filter keep stats: kept=%d assignee_match=%d reporter_match=%d external_assignee_match=%d",
            len(issues),
            matched_by_assignee,
            matched_by_reporter,
            matched_by_external_assignee,
        )

        if self._opt.limit_jira and self._opt.limit_jira > 0:
            issues = issues[: self._opt.limit_jira]
            log.info("JIRA: limiting to first %d issues for testing", self._opt.limit_jira)

        log.info("JIRA: [%d] issues were found", len(issues))

        cae_linux_keys = []
        for bug in issues:
            team_str = self.__get_team(bug)
            team_norm = str(team_str).lower()
            if "cae" in team_norm and "linux" in team_norm:
                cae_linux_keys.append(bug.key)
        if cae_linux_keys:
            log.info("JIRA fetch: CAE-Linux-like teams found: %d; sample keys: %s", len(cae_linux_keys), cae_linux_keys[:5])
        else:
            log.warning("JIRA fetch: no CAE-Linux-like TEAM values returned in query results")


        for bug in issues:
            jira_bug_list.append(self.__build_jira_bug_data(bug))

        if self._perf:
            log.info("JIRA perf summary (seconds):")
            for k in sorted(self._perf.keys()):
                log.info("  %-24s %8.2f (calls=%d)", k, self._perf[k], self._counts.get(k, 0))

        return jira_bug_list


def get_bug_project(_merged_bug: MergedBugData) -> str:
    """
    returns the bug project as listed in JIRA
    e.g. WIFI, BT, TOOL, ICPS
    """
    # in case we have a JIRA, so take the JIRA ID (before the '-')
    # if no JIRA, translate the 'ips_category' to match the JIRA convention
    # TODO: move this map to external json configuration file
    ips_category_jira_project_map = {
        "WiFi Windows": "WIFI",
        "Bluetooth (BT)": "BT",
        "Debug Tools": "DBGT",
        "OEM Tools": "WOT",
        "WCS Innovation Engineering": "CIE",
    }

    bug_project = DB_NA

    if _merged_bug.jira_data.jira_id != DB_NA:
        match = re.findall(r"(\w+)-", _merged_bug.jira_data.jira_id)
        if match:
            bug_project = match[0]
        else:
            # how did we get here?
            assert False, "failed to get project name based on JIRA ID"
    else:
        # no JIRA is found, get from 'ips_category'
        # if not found, set as 'NA'
        bug_project = ips_category_jira_project_map.get(_merged_bug.ips_data.ips_category, DB_NA)

    if bug_project == DB_NA:
        hsd_owner = (_merged_bug.hsd_data.hsd_owner or "").strip().lower()
        if hsd_owner == "timdaway":
            bug_project = "WIFI"
        elif hsd_owner == "yaochien":
            bug_project = "BT"

    if bug_project == DB_NA:
        log.info("JIRA: %s, IPS category: %s", _merged_bug.jira_data.jira_id, _merged_bug.ips_data.ips_category)

    return bug_project


def get_ips_tat_till_jira(_merged_bug: MergedBugData) -> int:
    """
    returns the IPS TAT till JIRA was filed (hours)
    assumption: JIRA exists
    """
    # sanity check
    assert _merged_bug.jira_data.jira_id != DB_NA, "JIRA doesn't exist"

    time_delta = _merged_bug.jira_data.jira_created_date - _merged_bug.ips_data.ips_created_date
    return round(time_delta.total_seconds() / 3600)


class DbConnector:
    """
    Postgres DB connector, to be merged later with DatabaseAPI module
    """

    def __init__(self):
        log.info("CTOR - DbConnector")
        self.__connection = None
        self.__cursor = None

        # open connection to DB
        self.__open_db_connection()

    def __del__(self):
        log.info("DTOR - DbConnector")
        try:
            if self.__cursor:
                self.__cursor.close()
        except Exception as e:
            log.warning("Failed to close DB cursor: %s", e)
        try:
            if self.__connection:
                self.__connection.close()
        except Exception as e:
            log.warning("Failed to close DB connection: %s", e)


    def __get_table_data(self, table: str) -> list:
        """
        returns all table data in a list of dict, where each element has
        key (field name): val (field data)
        assumption: table exists in DB
        WARNING: table name should be validated/whitelisted before calling this method
        """
        # Validate table name to prevent SQL injection (alphanumeric + underscore only)
        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            raise ValueError(f"Invalid table name: {table}")
        
        # run the query - using identifier quote for safety
        self.__cursor.execute(f'SELECT * FROM "{table}"')

        # get the results
        rows = self.__cursor.fetchall()

        # get column names
        col_names = [x[0] for x in self.__cursor.description]

        # arrange in a form of list of dictionaries (column: val)
        return list(dict(zip(col_names, list(result))) for result in rows)

    def get_table_rowcount(self, table: str) -> int:
        """Return row count for a table (validated name)."""

        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            raise ValueError(f"Invalid table name: {table}")

        self.__cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
        row = self.__cursor.fetchone()
        return int(row[0]) if row else 0

    def clear_hsd_snapshot_scope(self, table: str, owners: Sequence[str], start_date: date) -> int:
        """Clear stale HSD fields for a scoped reporter/year slice before refresh."""

        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            raise ValueError(f"Invalid table name: {table}")

        normalized_owners = sorted({str(owner).strip().lower() for owner in owners if str(owner).strip()})
        if not normalized_owners:
            return 0

        self.__cursor.execute(
            f'''
            UPDATE "{table}"
            SET
                hsd_id = %s,
                hsd_promoted_id = %s,
                hsd_status_reason = %s,
                hsd_customer_detail = %s,
                hsd_owner = %s,
                hsd_title = %s,
                hsd_submitted_date = %s,
                hsd_updated_date = %s,
                hsd_platform = %s,
                customer_closed_date = %s
            WHERE
                COALESCE(hsd_submitted_date::date, ips_created_date::date) >= %s
                AND LOWER(TRIM(COALESCE(reporter::text, ''))) = ANY(%s)
                AND LOWER(TRIM(COALESCE(hsd_owner::text, ''))) = LOWER(TRIM(COALESCE(reporter::text, '')))
                AND NULLIF(NULLIF(TRIM(hsd_id::text), ''), 'NA') IS NOT NULL
            ''',
            (
                DB_NA,
                DB_NA,
                DB_NA,
                DB_NA,
                DB_NA,
                DB_NA,
                DB_FUTURE_DATE,
                DB_FUTURE_DATE,
                DB_NA,
                DB_FUTURE_DATE,
                start_date,
                normalized_owners,
            ),
        )
        affected = self.__cursor.rowcount or 0
        self.__connection.commit()
        return int(affected)

    def update_hsd_columns(
        self,
        table: str,
        hsd_rows: list,
        insert_missing: bool = False,
        update_customer_from_hsd: bool = False,
    ) -> int:
        """Update HSD columns in an existing merged table (e.g., ips_jira_bugs).

        If insert_missing is True, unmatched HSD rows are appended as new rows
        (other IPS/JIRA fields remain defaults).
        """

        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            raise ValueError(f"Invalid table name: {table}")

        updated = 0
        skipped = 0
        skipped_keys: list[str] = []
        matched_info: list[str] = []
        skipped_info: list[str] = []
        unmatched_rows: list[MergedBugData] = []
        touched_jira_ids: set[str] = set()
        touched_hsd_ids: set[str] = set()
        touched_ips_case_numbers: set[int] = set()

        def _normalize_hsd_id(value: object) -> str:
            if value is None:
                return DB_NA
            text = str(value).strip()
            if not text or text.lower() == "na":
                return DB_NA
            return text

        customer_data: list[dict] = []
        if update_customer_from_hsd:
            try:
                customer_data = self.get_customers_data()
            except Exception as exc:
                log.warning("Failed to load customers data for HSD-only customer updates: %s", exc)
                customer_data = []

        debug_logged = 0
        def _compute_customer_value(bug: HsdBugData) -> str:
            if not update_customer_from_hsd:
                return DB_NA
            value = DB_NA
            if customer_data:
                merged = MergedBugData()
                merged.hsd_data = bug
                value = get_customer_name(customer_data, merged)
            hsd_detail = (getattr(bug, "hsd_customer_detail", "") or "").lower()
            if "microsoft" in hsd_detail:
                value = "MICROSOFT"
                log.debug(
                    "HSD-only customer override: hsd_id=%s customer_detail contains microsoft",
                    getattr(bug, "hsd_id", DB_NA),
                )
            return value

        for bug in hsd_rows:
            candidates: list[tuple[str, object]] = []

            promoted = _normalize_hsd_id(getattr(bug, "hsd_promoted_id", DB_NA))
            bug_id = _normalize_hsd_id(getattr(bug, "hsd_id", DB_NA))

            customer_value = _compute_customer_value(bug)
            if debug_logged < 10:
                log.debug(
                    "HSD-only customer calc: hsd_id=%s hsd_customer_detail=%s customer_value=%s",
                    bug_id,
                    getattr(bug, "hsd_customer_detail", DB_NA),
                    customer_value,
                )
                debug_logged += 1

            if promoted and promoted != DB_NA:
                candidates.append(("jira_id", promoted))
                if promoted.isdigit():
                    candidates.append(("ips_case_number", int(promoted)))
            if bug_id and bug_id != DB_NA:
                candidates.append(("jira_id", bug_id))
                candidates.append(("hsd_id", bug_id))

            normalized_hsd_status = normalize_hsd_status_reason(str(getattr(bug, "hsd_status_reason", "") or ""))
            is_hsd_closed = normalized_hsd_status == "closed"
            hsd_close_date = _null_db_date(getattr(bug, "hsd_updated_date", None)) if is_hsd_closed else None

            if not candidates:
                skipped += 1
                skipped_keys.append("<no-ids>")
                skipped_info.append("hsd_id=NA promoted=NA (no ids)")
                if insert_missing:
                    merged = MergedBugData()
                    merged.hsd_data = bug
                    merged.reporter = normalize_hsd_owner(getattr(bug, "hsd_owner", DB_NA))
                    merged.engineer = resolve_engineer_name(merged.reporter, DB_NA, DB_NA, DB_NA)
                    merged.customer = _compute_customer_value(bug)
                    unmatched_rows.append(merged)
                continue

            reporter_value = normalize_hsd_owner(getattr(bug, "hsd_owner", DB_NA))
            matched = False
            for field_name, key in candidates:
                if field_name == "jira_id":
                    self.__cursor.execute(
                        f'''
                        UPDATE "{table}"
                        SET
                            hsd_id = %s,
                            hsd_promoted_id = %s,
                            hsd_status_reason = %s,
                            hsd_customer_detail = %s,
                            hsd_owner = %s,
                            hsd_title = %s,
                            hsd_submitted_date = %s,
                            hsd_updated_date = %s,
                            hsd_platform = %s,
                            customer_closed_date = COALESCE(customer_closed_date, %s){',\n                            customer = %s' if update_customer_from_hsd and customer_value != DB_NA else ''}
                        WHERE UPPER(jira_id) = UPPER(%s)
                        ''',
                        (
                            getattr(bug, "hsd_id", DB_NA),
                            getattr(bug, "hsd_promoted_id", DB_NA),
                            getattr(bug, "hsd_status_reason", DB_NA),
                            getattr(bug, "hsd_customer_detail", DB_NA),
                            getattr(bug, "hsd_owner", DB_NA),
                            getattr(bug, "hsd_title", DB_NA),
                            _null_db_date(getattr(bug, "hsd_submitted_date", None)),
                            _null_db_date(getattr(bug, "hsd_updated_date", None)),
                            getattr(bug, "hsd_platform", DB_NA),
                            hsd_close_date,
                            *([customer_value] if update_customer_from_hsd and customer_value != DB_NA else []),
                            key,
                        ),
                    )
                elif field_name == "ips_case_number":
                    self.__cursor.execute(
                        f'''
                        UPDATE "{table}"
                        SET
                            hsd_id = %s,
                            hsd_promoted_id = %s,
                            hsd_status_reason = %s,
                            hsd_customer_detail = %s,
                            hsd_owner = %s,
                            hsd_title = %s,
                            hsd_submitted_date = %s,
                            hsd_updated_date = %s,
                            hsd_platform = %s,
                            customer_closed_date = COALESCE(customer_closed_date, %s){',\n                            customer = %s' if update_customer_from_hsd and customer_value != DB_NA else ''}
                        WHERE ips_case_number = %s
                        ''',
                        (
                            getattr(bug, "hsd_id", DB_NA),
                            getattr(bug, "hsd_promoted_id", DB_NA),
                            getattr(bug, "hsd_status_reason", DB_NA),
                            getattr(bug, "hsd_customer_detail", DB_NA),
                            getattr(bug, "hsd_owner", DB_NA),
                            getattr(bug, "hsd_title", DB_NA),
                            _null_db_date(getattr(bug, "hsd_submitted_date", None)),
                            _null_db_date(getattr(bug, "hsd_updated_date", None)),
                            getattr(bug, "hsd_platform", DB_NA),
                            hsd_close_date,
                            *([customer_value] if update_customer_from_hsd and customer_value != DB_NA else []),
                            key,
                        ),
                    )
                elif field_name == "hsd_id":
                    self.__cursor.execute(
                        f'''
                        UPDATE "{table}"
                        SET
                            hsd_id = %s,
                            hsd_promoted_id = %s,
                            hsd_status_reason = %s,
                            hsd_customer_detail = %s,
                            hsd_owner = %s,
                            hsd_title = %s,
                            hsd_submitted_date = %s,
                            hsd_updated_date = %s,
                            hsd_platform = %s,
                            customer_closed_date = COALESCE(customer_closed_date, %s){',\n                            customer = %s' if update_customer_from_hsd and customer_value != DB_NA else ''}
                        WHERE UPPER(hsd_id) = UPPER(%s)
                        ''',
                        (
                            getattr(bug, "hsd_id", DB_NA),
                            getattr(bug, "hsd_promoted_id", DB_NA),
                            getattr(bug, "hsd_status_reason", DB_NA),
                            getattr(bug, "hsd_customer_detail", DB_NA),
                            getattr(bug, "hsd_owner", DB_NA),
                            getattr(bug, "hsd_title", DB_NA),
                            _null_db_date(getattr(bug, "hsd_submitted_date", None)),
                            _null_db_date(getattr(bug, "hsd_updated_date", None)),
                            getattr(bug, "hsd_platform", DB_NA),
                            hsd_close_date,
                            *([customer_value] if update_customer_from_hsd and customer_value != DB_NA else []),
                            key,
                        ),
                    )
                else:
                    continue

                if self.__cursor.rowcount:
                    updated += self.__cursor.rowcount
                    matched_info.append(f"hsd_id={bug_id or 'NA'} promoted={promoted or 'NA'} via {field_name}={key}")
                    if field_name == "jira_id":
                        touched_jira_ids.add(str(key).strip().upper())
                    elif field_name == "ips_case_number":
                        try:
                            touched_ips_case_numbers.add(int(key))
                        except (TypeError, ValueError):
                            pass
                    elif field_name == "hsd_id":
                        touched_hsd_ids.add(str(key).strip().upper())
                    matched = True
                    break

            if not matched:
                skipped += 1
                skipped_keys.append(promoted or bug_id or "<none>")
                skipped_info.append(f"hsd_id={bug_id or 'NA'} promoted={promoted or 'NA'} (no match)")
                if insert_missing:
                    merged = MergedBugData()
                    merged.hsd_data = bug
                    merged.reporter = normalize_hsd_owner(getattr(bug, "hsd_owner", DB_NA))
                    merged.engineer = resolve_engineer_name(merged.reporter, DB_NA, DB_NA, DB_NA)
                    merged.customer = _compute_customer_value(bug)
                    if normalize_hsd_status_reason(str(getattr(bug, "hsd_status_reason", "") or "")) == "closed":
                        merged.customer_closed_date = getattr(bug, "hsd_updated_date", DB_FUTURE_DATE)
                    unmatched_rows.append(merged)

        # Keep engineer aligned when reporter/assignee sources are changed by HSD refresh.
        engineer_refreshed = 0
        if update_customer_from_hsd and table == "ips_jira_bugs" and (touched_jira_ids or touched_hsd_ids or touched_ips_case_numbers):
            try:
                where_clauses: list[str] = []
                params: list[object] = []
                if touched_jira_ids:
                    where_clauses.append("UPPER(COALESCE(jira_id::text, '')) = ANY(%s)")
                    params.append(sorted(touched_jira_ids))
                if touched_hsd_ids:
                    where_clauses.append("UPPER(COALESCE(hsd_id::text, '')) = ANY(%s)")
                    params.append(sorted(touched_hsd_ids))
                if touched_ips_case_numbers:
                    where_clauses.append("ips_case_number = ANY(%s)")
                    params.append(sorted(touched_ips_case_numbers))

                self.__cursor.execute(
                    f'''
                    SELECT ctid, reporter, jira_assignee, jira_external_assignee, jira_team, engineer
                    FROM "{table}"
                    WHERE {' OR '.join(where_clauses)}
                    ''',
                    tuple(params),
                )
                fetched_rows = self.__cursor.fetchall()
                columns = [desc[0] for desc in self.__cursor.description]

                for result in fetched_rows:
                    row = dict(zip(columns, result))
                    new_engineer = resolve_engineer_name(
                        row.get("reporter", DB_NA),
                        row.get("jira_assignee", DB_NA),
                        row.get("jira_external_assignee", DB_NA),
                        row.get("jira_team", DB_NA),
                    )
                    old_engineer = row.get("engineer", DB_NA)
                    if (old_engineer or DB_NA) == (new_engineer or DB_NA):
                        continue
                    self.__cursor.execute(
                        f'UPDATE "{table}" SET engineer = %s WHERE ctid = %s',
                        (new_engineer, row["ctid"]),
                    )
                    engineer_refreshed += self.__cursor.rowcount or 0
            except Exception as exc:
                log.warning("Failed to refresh engineer after HSD update in %s: %s", table, exc)

        if update_customer_from_hsd and table == "ips_jira_bugs":
            try:
                self.__cursor.execute(
                    f'''
                    SELECT ctid, reporter, jira_assignee, jira_external_assignee, jira_team
                    FROM "{table}"
                    WHERE NULLIF(TRIM(COALESCE(hsd_id::text, '')), '') IS NOT NULL
                      AND UPPER(TRIM(COALESCE(engineer::text, 'NA'))) = 'NA'
                    '''
                )
                missing_rows = self.__cursor.fetchall()
                for ctid, reporter, jira_assignee, jira_external_assignee, jira_team in missing_rows:
                    new_engineer = resolve_engineer_name(
                        reporter,
                        jira_assignee,
                        jira_external_assignee,
                        jira_team,
                    )
                    if new_engineer in (None, "", DB_NA):
                        continue
                    self.__cursor.execute(
                        f'UPDATE "{table}" SET engineer = %s WHERE ctid = %s',
                        (new_engineer, ctid),
                    )
                    engineer_refreshed += self.__cursor.rowcount or 0
            except Exception as exc:
                log.warning("Failed HSD NA engineer safety-net in %s: %s", table, exc)

        self.__connection.commit()
        inserted = 0
        if insert_missing and unmatched_rows:
            opt = RunOptions()
            opt.db_recreate_table = False
            opt.db_use_batch_insert = True
            try:
                self.insert_to_table(table, unmatched_rows, False, opt=opt)
                inserted = len(unmatched_rows)
            except Exception as exc:
                log.warning("Failed to insert unmatched HSD rows into %s: %s", table, exc)

        sample = ", ".join(skipped_keys[:5]) if skipped_keys else ""
        log.info(
            "HSD refresh in %s: updated %d row(s); engineer refreshed %d; inserted %d new; skipped %d unmatched%s",
            table,
            updated,
            engineer_refreshed,
            inserted,
            skipped,
            f" (sample unmatched: {sample})" if sample else "",
        )
        if matched_info:
            log.info("HSD matches: %s", "; ".join(matched_info[:10]))
        if skipped_info:
            log.info("HSD unmatched details: %s", "; ".join(skipped_info[:10]))
        return updated + inserted

    def backfill_engineer_for_table(
        self,
        table: str = "ips_jira_bugs",
        only_missing: bool = False,
        dry_run: bool = False,
    ) -> tuple[int, int]:
        """Recompute engineer values from reporter/assignee sources for an existing table."""

        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            raise ValueError(f"Invalid table name: {table}")

        where_sql = ""
        if only_missing:
            where_sql = "WHERE UPPER(TRIM(COALESCE(engineer::text, 'NA'))) = 'NA'"

        self.__cursor.execute(
            f'''
            SELECT ctid, reporter, jira_assignee, jira_external_assignee, jira_team, engineer
            FROM "{table}"
            {where_sql}
            '''
        )
        rows = self.__cursor.fetchall()
        checked = len(rows)
        updated = 0

        def _norm_text(value: object) -> str:
            text = str(value or "").strip()
            return DB_NA if not text else text

        for ctid, reporter, jira_assignee, jira_external_assignee, jira_team, old_engineer in rows:
            new_engineer = resolve_engineer_name(
                reporter,
                jira_assignee,
                jira_external_assignee,
                jira_team,
            )

            if _norm_text(old_engineer) == _norm_text(new_engineer):
                continue

            if not dry_run:
                self.__cursor.execute(
                    f'UPDATE "{table}" SET engineer = %s WHERE ctid = %s',
                    (new_engineer, ctid),
                )
                updated += self.__cursor.rowcount or 0
            else:
                updated += 1

        if not dry_run:
            self.__connection.commit()

        log.info(
            "Engineer backfill in %s: checked=%d changed=%d (only_missing=%s, dry_run=%s)",
            table,
            checked,
            updated,
            only_missing,
            dry_run,
        )
        return checked, updated

    def get_customers_data(self) -> list:
        """
        returns all data from customers table
        """
        return self.__get_table_data("customers")

    def query_rows(self, query: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
        """Run a read-only SELECT query and return rows as list of dicts."""
        if not re.match(r"^\s*(select|with)\b", query, re.IGNORECASE):
            raise ValueError("Only SELECT queries are allowed in query_rows().")
        self.__cursor.execute(query, params or None)
        rows = self.__cursor.fetchall()
        col_names = [x[0] for x in self.__cursor.description] if self.__cursor.description else []
        return [dict(zip(col_names, list(result))) for result in rows]

    def __open_db_connection(self):
        """
        open the connection to the postgres customer engineering DB
        stores:
        >>> conn: a new instance of the connection class
        >>> cur : cursor to execute any SQL statements
        """
        connect_kwargs = {
            "database": Sherlock.PostgresCustomerEngineeringDb.database,
            "user": Sherlock.PostgresCustomerEngineeringDb.user,
            "password": Sherlock.PostgresCustomerEngineeringDb.password,
            "host": Sherlock.PostgresCustomerEngineeringDb.host,
            "port": Sherlock.PostgresCustomerEngineeringDb.port,
        }
        try:
            connection = psycopg2.connect(**connect_kwargs)
        except Exception as exc:
            if 'invalid connection option "database"' not in str(exc):
                raise
            connect_kwargs["dbname"] = connect_kwargs.pop("database")
            connection = psycopg2.connect(**connect_kwargs)

        cursor = connection.cursor()
        self.__connection = connection
        self.__cursor = cursor

        try:
            cursor.execute("SELECT current_database(), current_schema(), current_setting('search_path')")
            db_name, schema_name, search_path = cursor.fetchone()
            log.info(
                "DB connection info: host=%s db=%s schema=%s search_path=%s",
                Sherlock.PostgresCustomerEngineeringDb.host,
                db_name,
                schema_name,
                search_path,
            )
        except Exception as exc:  # pragma: no cover - best-effort diagnostics
            log.warning("Could not log DB connection info: %s", exc)

        

    def __delete_table(self, table_name: str) -> None:
        """
        delete a table from DB
        caution: all data will be erased, requires SO (schema owner) permissions
        """
        # Validate table name to prevent SQL injection
        if not re.match(r'^[a-zA-Z0-9_]+$', table_name):
            raise ValueError(f"Invalid table name: {table_name}")
        
        log.info("delete table: %s", table_name)
        # Keep dependent views (e.g. un_promoted_hsd) intact; fallback to TRUNCATE is handled by caller
        self.__cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')

        # commit changes
        self.__connection.commit()

    def __create_table(self, table_name: str, table_structure: list) -> None:
        """
        create a new table in the DB
        assumption: 'table_name' doesn't exist
        caution: requires SO (schema owner) permissions
        INSERT INTO
            table_name (col1, col2)
        VALUES
            (val11, val12),
            (val21, val22)
        """
        # Validate table name to prevent SQL injection
        if not re.match(r'^[a-zA-Z0-9_]+$', table_name):
            raise ValueError(f"Invalid table name: {table_name}")
        
        # generate all columns
        columns_str = ""
        for entry in table_structure:
            field_name = entry["field_name"]
            field_type = entry["field_type"]
            
            # Validate field names to prevent SQL injection
            if not re.match(r'^[a-zA-Z0-9_]+$', field_name):
                raise ValueError(f"Invalid field name: {field_name}")
            # Validate field types (whitelist common PostgreSQL types)
            valid_types = ['TEXT', 'INT', 'BOOLEAN', 'TIMESTAMP', 'VARCHAR', 'DATE', 'FLOAT', 'BIGINT']
            if not any(field_type.upper().startswith(vt) for vt in valid_types):
                raise ValueError(f"Invalid field type: {field_type}")

            columns_str += f"{field_name} {field_type},"

        # remove last comma
        columns_str = columns_str[:-1]

        # generate final query
        query_string = f"CREATE TABLE {table_name} ({columns_str})"

        # create a table
        log.info("create a new table using the following query:")
        log.info(query_string)
        self.__cursor.execute(query_string)

        # commit changes
        self.__connection.commit()

    def __get_table_fields_for_db(self, data_obj: object) -> list:
        """
        generates list with field names, values and filed types of a table
        based on the data_obj which is a dataclass (can be nested)
        e.g. temp_data_list = [
            {"field_name": col1, "field_type": INT, "field_val": 1},
            {"field_name": col2, "field_type": TEXT, "field_val": "some text"},
        ]
        """
        temp_data_list = []

        for _field in fields(data_obj):
            # check if this field is a data class
            # if yes, call same func recursively
            if is_dataclass(_field.type):
                temp_data_list += self.__get_table_fields_for_db(getattr(data_obj, _field.name))
            else:
                # primitive variable
                val = getattr(data_obj, _field.name)
                key_type = ""
                if isinstance(val, datetime):
                    key_type = "TIMESTAMP"
                    val = _null_db_date(val)
                elif isinstance(val, str):
                    key_type = "TEXT"
                    # do not allow empty strings
                    if not val:
                        val = DB_NA
                    # single quotes are not allowed
                    val = val.replace("'", "")
                    # remove non-ENG chars (PBI doesn't like it)
                    val = val.encode(encoding="ascii", errors="ignore").decode()
                elif isinstance(val, bool):
                    key_type = "BOOLEAN"
                elif isinstance(val, int):
                    key_type = "INT"
                elif isinstance(val, float):
                    if pd.isna(val):
                        val = DB_NA
                        key_type = "TEXT"
                    elif float(val).is_integer():
                        val = int(val)
                        key_type = "INT"
                    else:
                        key_type = "FLOAT"
                elif val is None:
                    val = DB_NA
                    key_type = "TEXT"
                else:
                    # Fallback for object-like payloads (e.g. Jira option objects).
                    obj_text = getattr(val, "name", None) or getattr(val, "value", None) or str(val)
                    val = str(obj_text)
                    if not val:
                        val = DB_NA
                    val = val.replace("'", "")
                    val = val.encode(encoding="ascii", errors="ignore").decode()
                    key_type = "TEXT"

                temp_data_list.append(
                    {
                        "field_name": _field.name,
                        "field_type": key_type,
                        "field_val": val,
                    }
                )

        return temp_data_list

    def insert_to_table(
        self,
        table_name: str,
        value_dict: list,
        is_delete_existing_table: bool,
        opt: RunOptions | None = None,
    ) -> None:
        """
        insert the whole list of dataclass rows into the table
        """
        if not value_dict:
            log.warning("insert_to_table: no rows to insert")
            return

        if opt is None:
            opt = RunOptions()

        # Validate table name early
        if not re.match(r'^[a-zA-Z0-9_]+$', table_name):
            raise ValueError(f"Invalid table name: {table_name}")

        def _get_existing_columns(_table_name: str) -> list[str]:
            self.__cursor.execute(
                """
                                SELECT a.attname
                                FROM pg_attribute a
                                JOIN pg_class c ON a.attrelid = c.oid
                                WHERE c.relname = %s
                                    AND pg_table_is_visible(c.oid)
                                    AND a.attnum > 0
                                    AND NOT a.attisdropped
                                ORDER BY a.attnum
                """,
                (_table_name,),
            )
            return [row[0] for row in self.__cursor.fetchall()]

        def _add_missing_columns(_table_name: str, _missing_columns: list[dict]) -> None:
            valid_types = ["TEXT", "INT", "BOOLEAN", "TIMESTAMP", "VARCHAR", "DATE", "FLOAT", "BIGINT"]
            for entry in _missing_columns:
                field_name = entry["field_name"]
                field_type = entry["field_type"]
                if not re.match(r'^[a-zA-Z0-9_]+$', field_name):
                    raise ValueError(f"Invalid field name: {field_name}")
                if not any(field_type.upper().startswith(vt) for vt in valid_types):
                    raise ValueError(f"Invalid field type: {field_type}")
                self.__cursor.execute(
                    f'ALTER TABLE "{_table_name}" ADD COLUMN IF NOT EXISTS "{field_name}" {field_type}'
                )
            self.__connection.commit()

        # infer table structure from first row
        row_schema = self.__get_table_fields_for_db(value_dict[0])
        tbl_parsed = row_schema
        field_names = [entry["field_name"] for entry in tbl_parsed]
        recreated_table = False

        if not is_delete_existing_table:
            existing_columns = _get_existing_columns(table_name)
            if existing_columns:
                missing_columns = [entry for entry in row_schema if entry["field_name"] not in set(existing_columns)]
                if missing_columns and opt.db_auto_add_missing_columns:
                    _add_missing_columns(table_name, missing_columns)
                    existing_columns = _get_existing_columns(table_name)
                    log.info(
                        "Table %s auto-added %d missing column(s): %s",
                        table_name,
                        len(missing_columns),
                        [entry["field_name"] for entry in missing_columns],
                    )

                existing_column_set = set(existing_columns)
                tbl_parsed = [entry for entry in row_schema if entry["field_name"] in existing_column_set]
                field_names = [entry["field_name"] for entry in tbl_parsed]
                dropped_columns = [entry for entry in row_schema if entry["field_name"] not in existing_column_set]
                if dropped_columns:
                    dropped_names = [entry["field_name"] for entry in dropped_columns]
                    log.warning(
                        "Table %s is missing %d column(s); skipping them for insert: %s",
                        table_name,
                        len(dropped_names),
                        dropped_names,
                    )
        
        # Validate all field names
        for fname in field_names:
            if not re.match(r'^[a-zA-Z0-9_]+$', fname):
                raise ValueError(f"Invalid field name: {fname}")

        if not field_names:
            raise ValueError(f"No insertable columns for table: {table_name}")
        
        columns_sql = f"({', '.join(field_names)})"

        # For ips_jira_bugs, update existing rows instead of appending duplicates.
        if table_name == "ips_jira_bugs" and not is_delete_existing_table:
            ips_cases: set[str] = set()
            jira_ids: set[str] = set()
            hsd_ids: set[str] = set()

            for obj in value_dict:
                parsed = self.__get_table_fields_for_db(obj)
                row_map = {entry["field_name"]: entry["field_val"] for entry in parsed}

                ips_val = row_map.get("ips_case_number")
                try:
                    if ips_val not in (None, "", DB_NA):
                        ips_num = int(ips_val)
                        if ips_num > 0:
                            ips_cases.add(str(ips_num))
                except (TypeError, ValueError):
                    pass

                jira_val = row_map.get("jira_id")
                if jira_val not in (None, "", DB_NA):
                    jira_ids.add(str(jira_val).strip().lower())

                hsd_val = row_map.get("hsd_id")
                if hsd_val not in (None, "", DB_NA):
                    hsd_ids.add(str(hsd_val).strip().lower())

            where_parts = []
            params: list[Any] = []
            existing_column_set = set(field_names)
            if ips_cases and "ips_case_number" in existing_column_set:
                where_parts.append("NULLIF(NULLIF(TRIM(ips_case_number::text), ''), 'NA') = ANY(%s)")
                params.append(list(ips_cases))
            if jira_ids and "jira_id" in existing_column_set:
                where_parts.append("LOWER(NULLIF(NULLIF(TRIM(jira_id::text), ''), 'NA')) = ANY(%s)")
                params.append(list(jira_ids))
            if hsd_ids and "hsd_id" in existing_column_set:
                where_parts.append("LOWER(NULLIF(NULLIF(TRIM(hsd_id::text), ''), 'NA')) = ANY(%s)")
                params.append(list(hsd_ids))

            if where_parts:
                delete_sql = f"DELETE FROM {table_name} WHERE " + " OR ".join(where_parts)
                self.__cursor.execute(delete_sql, params)
                self.__connection.commit()

        # delete existing table if needed
        if is_delete_existing_table:
            try:
                self.__delete_table(table_name)
                self.__create_table(table_name, tbl_parsed)
                recreated_table = True
                log.info("Recreated table %s before insert", table_name)
            except psycopg2.errors.DependentObjectsStillExist:
                # A dependent view exists; fall back to TRUNCATE to preserve views.
                log.warning(
                    "Dependent objects found for %s; falling back to TRUNCATE to preserve views.",
                    table_name,
                )
                self.__connection.rollback()
                self.__cursor.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY')
                self.__connection.commit()
            except Exception as exc:
                # If recreate fails for any other reason, try a truncate so we never append.
                log.warning("Failed to recreate %s (%s); attempting TRUNCATE instead.", table_name, exc)
                self.__connection.rollback()
                self.__cursor.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY')
                self.__connection.commit()

        # If recreate was requested but we had to fall back to TRUNCATE,
        # align insert columns with existing table and add missing ones when allowed.
        if is_delete_existing_table and not recreated_table:
            existing_columns = _get_existing_columns(table_name)
            if existing_columns:
                missing_columns = [entry for entry in row_schema if entry["field_name"] not in set(existing_columns)]
                if missing_columns and opt.db_auto_add_missing_columns:
                    _add_missing_columns(table_name, missing_columns)
                    existing_columns = _get_existing_columns(table_name)
                    log.info(
                        "Table %s auto-added %d missing column(s) after TRUNCATE fallback: %s",
                        table_name,
                        len(missing_columns),
                        [entry["field_name"] for entry in missing_columns],
                    )

                existing_column_set = set(existing_columns)
                tbl_parsed = [entry for entry in row_schema if entry["field_name"] in existing_column_set]
                field_names = [entry["field_name"] for entry in tbl_parsed]
                dropped_columns = [entry for entry in row_schema if entry["field_name"] not in existing_column_set]
                if dropped_columns:
                    dropped_names = [entry["field_name"] for entry in dropped_columns]
                    log.warning(
                        "Table %s is missing %d column(s) after TRUNCATE fallback; skipping them for insert: %s",
                        table_name,
                        len(dropped_names),
                        dropped_names,
                    )

        if opt.db_use_batch_insert:
            # Build rows as tuples (let psycopg2 adapt types)
            rows = []
            for obj in value_dict:
                parsed = self.__get_table_fields_for_db(obj)
                parsed_map = {x["field_name"]: x["field_val"] for x in parsed}
                rows.append(tuple(parsed_map.get(name) for name in field_names))

            insert_sql = f"INSERT INTO {table_name} {columns_sql} VALUES %s"
            log.info("update table with relevant data (batch insert) ..")
            execute_values(
                self.__cursor,
                insert_sql,
                rows,
                page_size=opt.db_batch_page_size,
            )
            self.__connection.commit()
            log.info("update is complete. %s rows were inserted", len(rows))
            try:
                self.__cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
                rowcount = self.__cursor.fetchone()[0]
                log.info("table %s rowcount after insert: %s", table_name, rowcount)
            except Exception as exc:
                log.warning("Could not fetch rowcount for %s: %s", table_name, exc)
            return

        # fallback: your original row-by-row insertion (slow)
        sql_strings = []
        for obj in value_dict:
            single_raw_data = []
            parsed = self.__get_table_fields_for_db(obj)
            parsed_map = {x["field_name"]: x["field_val"] for x in parsed}
            for col_name in field_names:
                field_val = parsed_map.get(col_name)
                if field_val is None:
                    single_raw_data.append("NULL")
                else:
                    single_raw_data.append(f"'{field_val}'")
            sql_strings.append(f"({', '.join(single_raw_data)})")

        log.info("update table with relevant data (row-by-row; slow) ..")
        for row in sql_strings:
            query_string = f"INSERT INTO {table_name} {columns_sql} VALUES {row}"
            self.__cursor.execute(query_string)

        self.__connection.commit()
        log.info("update is complete. %s rows were inserted", len(sql_strings))
        try:
            self.__cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            rowcount = self.__cursor.fetchone()[0]
            log.info("table %s rowcount after insert: %s", table_name, rowcount)
        except Exception as exc:
            log.warning("Could not fetch rowcount for %s: %s", table_name, exc)



_IPS_ONLY_TITLE_BRAND_RULES: list[tuple[str, str]] = [
    ("MICROSOFT", r"(^|[^A-Za-z0-9])MSFT([^A-Za-z0-9]|$)"),
    ("MICROSOFT", r"(^|[^A-Za-z0-9])MICROSOFT([^A-Za-z0-9]|$)"),
    ("MADIGI", r"(^|[^A-Za-z0-9])MADIG(I)?([^A-Za-z0-9]|$)"),
    ("BYD", r"(^|[^A-Za-z0-9])BYD([^A-Za-z0-9]|$)"),
    ("MECHREVO", r"(^|[^A-Za-z0-9])MECHREVO([^A-Za-z0-9]|$)"),
    ("EPSON", r"(^|[^A-Za-z0-9])EPSON([^A-Za-z0-9]|$)"),
    ("HAIER", r"(^|[^A-Za-z0-9])HAIER([^A-Za-z0-9]|$)"),
    ("INTEL", r"(^|[^A-Za-z0-9])INTEL([^A-Za-z0-9]|$)"),
    ("WEIBU", r"(^|[^A-Za-z0-9])WEIBU([^A-Za-z0-9]|$)"),
    ("AISTONE", r"(^|[^A-Za-z0-9])AISTONE([^A-Za-z0-9]|$)|(^|[^A-Za-z0-9])ALSTONE([^A-Za-z0-9]|$)"),
    ("EMDOOR", r"(^|[^A-Za-z0-9])EMDOOR([^A-Za-z0-9]|$)"),
    ("SIXUNITED", r"(^|[^A-Za-z0-9])SIXUNITED([^A-Za-z0-9]|$)"),
    ("UNIWILL", r"(^|[^A-Za-z0-9])UNIWILL([^A-Za-z0-9]|$)"),
    ("HUAWEI", r"(^|[^A-Za-z0-9])HUAWEI([^A-Za-z0-9]|$)"),
    ("HONOR", r"(^|[^A-Za-z0-9])HONOR([^A-Za-z0-9]|$)"),
    ("HONOR", r"(^|[^A-Za-z0-9])HONORI([^A-Za-z0-9]|$)"),
    ("SAMSUNG", r"(^|[^A-Za-z0-9])SAMSUNG([^A-Za-z0-9]|$)"),
    ("XIAOMI", r"(^|[^A-Za-z0-9])XIAOMI([^A-Za-z0-9]|$)"),
    ("REALME", r"(^|[^A-Za-z0-9])REALME([^A-Za-z0-9]|$)"),
    ("OPPO", r"(^|[^A-Za-z0-9])OPPO([^A-Za-z0-9]|$)"),
    ("LENOVO", r"(^|[^A-Za-z0-9])LENOVO([^A-Za-z0-9]|$)"),
    ("LENOVO", r"(^|[^A-Za-z0-9])YOGA([^A-Za-z0-9]|$)"),
    ("LENOVO", r"(^|[^A-Za-z0-9])THINKPAD([^A-Za-z0-9]|$)"),
    ("LENOVO", r"(^|[^A-Za-z0-9])IDEAPAD([^A-Za-z0-9]|$)"),
    ("VAIO", r"(^|[^A-Za-z0-9])VAIO([^A-Za-z0-9]|$)"),
    ("NEC", r"(^|[^A-Za-z0-9])NEC([^A-Za-z0-9]|$)"),
    ("MSI", r"(^|[^A-Za-z0-9])MSI([^A-Za-z0-9]|$)"),
    ("UNIS", r"(^|[^A-Za-z0-9])UNIS([^A-Za-z0-9]|$)"),
    ("CLEVO", r"(^|[^A-Za-z0-9])CLEVO([^A-Za-z0-9]|$)"),
    ("ASROCK", r"(^|[^A-Za-z0-9])ASROCK([^A-Za-z0-9]|$)"),
    ("RAZER", r"(^|[^A-Za-z0-9])RAZER([^A-Za-z0-9]|$)"),
    ("GIGABYTE", r"(^|[^A-Za-z0-9])GIGABYTE([^A-Za-z0-9]|$)"),
    ("ACER", r"(^|[^A-Za-z0-9])ACER([^A-Za-z0-9]|$)"),
    ("ASUS", r"(^|[^A-Za-z0-9])ASUS([^A-Za-z0-9]|$)"),
    ("HP", r"(^|[^A-Za-z0-9])HP([^A-Za-z0-9]|$)"),
    ("DELL", r"(^|[^A-Za-z0-9])DELL([^A-Za-z0-9]|$)"),
    ("LG", r"(^|[^A-Za-z0-9])LG([^A-Za-z0-9]|$)"),
    ("PANASONIC", r"(^|[^A-Za-z0-9])PANASONIC([^A-Za-z0-9]|$)"),
    ("TOSHIBA", r"(^|[^A-Za-z0-9])TOSHIBA([^A-Za-z0-9]|$)"),
    ("FUJITSU", r"(^|[^A-Za-z0-9])FUJITSU([^A-Za-z0-9]|$)"),
    ("WIKO", r"(^|[^A-Za-z0-9])WIKO([^A-Za-z0-9]|$)"),
    ("INTEL NUC", r"(^|[^A-Za-z0-9])INTEL[[:space:]]+NUC([^A-Za-z0-9]|$)|(^|[^A-Za-z0-9])NUC([^A-Za-z0-9]|$)"),
]


def _is_ips_only_bug(ips_case_number: object, jira_id: object) -> bool:
    try:
        ips_case = int(ips_case_number or 0)
    except (TypeError, ValueError):
        ips_case = 0
    if ips_case <= 0:
        return False

    jira_key = str(jira_id or "").strip()
    if not jira_key or jira_key.upper() == DB_NA:
        return True

    return not bool(re.match(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$", jira_key))


def _infer_ips_only_customer_from_title(title: object) -> str:
    if title is None:
        return DB_NA
    if isinstance(title, float) and pd.isna(title):
        return DB_NA

    title_text = str(title).strip()
    if not title_text or title_text.upper() == DB_NA:
        return DB_NA

    for target_customer, regex_pattern in _IPS_ONLY_TITLE_BRAND_RULES:
        if re.search(regex_pattern, title_text, flags=re.IGNORECASE):
            return _canonicalize_customer_name(target_customer)

    return DB_NA


def get_customer_name(_customer_data: list, _merged_bug: MergedBugData) -> str:
    """
    deduce the customer possible names from the bug_data, based on customer_data DB
    """
    # dict where key (str) is the customer name and value (int) is the number of times it appeared
    # this is needed in case we found multiple customers for same bug, we take the one which appears the most
    customers_dict = {}

    # get bug numbers for prints only
    ips_case_number = _merged_bug.ips_data.ips_case_number
    jira_id = _merged_bug.jira_data.jira_id

    ips_title_text = str(_merged_bug.ips_data.ips_title or "")
    jira_title_text = str(_merged_bug.jira_data.jira_title or "")
    if re.search(r"\bsurface\b", ips_title_text, flags=re.IGNORECASE) or re.search(
        r"\bsurface\b", jira_title_text, flags=re.IGNORECASE
    ):
        return "MICROSOFT"

    is_ips_only = _is_ips_only_bug(ips_case_number, jira_id)

    # get all the fields which might contain customer name
    raw_fields = [
        _merged_bug.ips_data.ips_title,
        _merged_bug.jira_data.jira_title,
        _merged_bug.jira_data.jira_customer_name,
        _merged_bug.ips_data.ips_oem,
        _merged_bug.ips_data.ips_odm,
        _merged_bug.ips_data.ips_reporter_account_name,
            _merged_bug.hsd_data.hsd_title,
            _merged_bug.hsd_data.hsd_customer_detail,
    ]

    str_list: list[str] = []
    for value in raw_fields:
        if value is None:
            str_list.append("")
            continue
        if isinstance(value, float) and pd.isna(value):
            str_list.append("")
            continue
        str_list.append(str(value))

    # concat together
    merged_str = " ".join(str_list).lower()

    # replace all special chars (everything but letters) by spaces
    # e.g. [dell][quanta] some bug --> dell quanta some bug
    clean_str = re.sub("[^a-z]+", " ", merged_str)

    # divide into words list
    words_list = clean_str.split(" ")

    for customer in _customer_data:
        # search by OEM only
        if customer["customer_role"] == "oem":
            customer_name = customer["customer_name"]
            # keywords are additional potential names
            customer_keywords = customer["customer_keywords"]
            customer_keywords_list = [] if not customer_keywords else customer_keywords.split(",")

            for name in [customer_name] + customer_keywords_list:
                for word in words_list:
                    # look for exact match
                    if name == word:
                        if customer_name not in customers_dict:
                            customers_dict[customer_name] = 0
                        customers_dict[customer_name] += 1

    if not customers_dict:
        if is_ips_only:
            inferred_customer = _infer_ips_only_customer_from_title(_merged_bug.ips_data.ips_title)
            if inferred_customer != DB_NA:
                log.debug(
                    "customer inferred from IPS-only title for IPS [%s], JIRA [%s]: %s",
                    ips_case_number,
                    jira_id,
                    inferred_customer,
                )
                return _canonicalize_customer_name(inferred_customer)
            log.debug(
                "customer fallback to %s for IPS-only issue with no brand match. IPS [%s], JIRA [%s]",
                OTHER_OEM_CUSTOMER,
                ips_case_number,
                jira_id,
            )
            return OTHER_OEM_CUSTOMER

        log.info("no customer was found for IPS [%s], JIRA [%s]", ips_case_number, jira_id)
        return DB_NA

    # get the max value in case we have multiple customers
    final_customer = _canonicalize_customer_name(max(customers_dict, key=customers_dict.get))

    if len(customers_dict) > 1:
        # this is probably not a bug, but it is always better that we have only one
        log.debug(
            "multiple customers were found for IPS [%s], JIRA [%s], choosing %s",
            ips_case_number,
            jira_id,
            final_customer,
        )
        log.debug(customers_dict)

    if is_ips_only:
        inferred_customer = _infer_ips_only_customer_from_title(_merged_bug.ips_data.ips_title)
        inferred_customer = _canonicalize_customer_name(inferred_customer)
        if inferred_customer != DB_NA and inferred_customer != final_customer:
            log.debug(
                "customer overridden by IPS-only title rule for IPS [%s], JIRA [%s]: %s -> %s",
                ips_case_number,
                jira_id,
                final_customer,
                inferred_customer,
            )
            final_customer = inferred_customer

    # in case of multiple customers, take the one appears the most
    return final_customer


def get_employee_name_from_email(email: object) -> str:
    """
    returns the employee name from the email address
    e.g. Roi Fridburg, from roi.fridburg@intel.com
    """
    if email is None:
        return DB_NA

    # pandas/snowflake merges can yield NaN floats; treat as unknown.
    if isinstance(email, float) and pd.isna(email):
        return DB_NA

    email_text = str(email).strip()
    if not email_text or email_text.upper() == "NA":
        return DB_NA

    # need to get rid of the middle name
    if "@" in email_text:
        names = email_text.split("@")[0].split(".")
        # take the first and last indices
        return f"{names[0].capitalize()} {names[-1].capitalize()}"

    # we dont have an email, employee is unknown
    return DB_NA


_HSD_OWNER_DISPLAY_MAP = {
    "yaochien": "Leo Chiang",
    "leo chiang": "Leo Chiang",
    "timdaway": "Timdaway Lai",
    "timdaway lai": "Timdaway Lai",
    "szchen": "Steven1 Chen",
    "steven1 chen": "Steven1 Chen",
    "jtsao1": "Jonathan Tsao",
    "jonathan tsao": "Jonathan Tsao",
    "yuweich1": "Yu-Wei Chen",
    "yu-wei chen": "Yu-Wei Chen",
    "frankfcy": "Frank Yang",
    "frank yang": "Frank Yang",
    "brentonw": "Brenton Wu",
    "brenton wu": "Brenton Wu",
    "chenmatt": "Matt Chen",
    "matt chen": "Matt Chen",
    "caizhiqi": "Zhiqiang Cai",
    "zhiqiang cai": "Zhiqiang Cai",
    "flee5": "Frank Lee",
    "frank lee": "Frank Lee",
    "wesleyku": "Wesley Kuo",
    "wesley kuo": "Wesley Kuo",
    "bingyues": "Bingyue Sun",
    "bingyue sun": "Bingyue Sun",
    "jzou6": "Juan Zou",
    "juan zou": "Juan Zou",
    "chuchar1": "Charles Chu",
    "charles chu": "Charles Chu",
}


_IPS_OWNER_REPORTER_ALIAS_MAP = {
    "franky yang": "Frank Yang",
    "charles p chu": "Charles Chu",
    "steven zy chen": "Steven Chen",
}


_JIRA_ASSIGNEE_ALIAS_MAP = {
    "jackx lee": "Jackx Lee",
    "lydiax chien": "Lydiax Chien",
    "johnsonx su": "Johnsonx Su",
    "xihaox yang": "Xihaox Yang",
    "henryx su": "Henryx Su",
    "tonyx yeh": "Tonyx Yeh",
    "yu wei chen": "Yu-Wei Chen",
    "yu-wei chen": "Yu-Wei Chen",
    "kj fang": "KJ Fang",
}


def _smart_title_name(name: str) -> str:
    """Title-case a name while preserving all-uppercase tokens and hyphenated parts."""

    def _title_token(token: str) -> str:
        if not token:
            return token
        if token.isupper() and len(token) <= 3:
            return token
        if "-" in token:
            return "-".join(_title_token(part) for part in token.split("-"))
        return token.capitalize()

    return " ".join(_title_token(tok) for tok in name.split())


def normalize_hsd_owner(owner: str) -> str:
    """Map HSD owner aliases to human-friendly display names."""

    if not owner or owner == DB_NA:
        return DB_NA

    owner_clean = owner.strip()
    if not owner_clean:
        return DB_NA

    mapped = _HSD_OWNER_DISPLAY_MAP.get(owner_clean.lower())
    if mapped:
        return mapped

    if "@" in owner_clean:
        return get_employee_name_from_email(owner_clean)

    return owner_clean


def normalize_ips_owner_reporter(owner: object) -> str:
    """Normalize IPS owner name into reporter display name."""

    if owner is None:
        return DB_NA

    if isinstance(owner, float) and pd.isna(owner):
        return DB_NA

    owner_clean = str(owner).strip()
    if not owner_clean or owner_clean.upper() == "NA":
        return DB_NA

    mapped = _IPS_OWNER_REPORTER_ALIAS_MAP.get(owner_clean.lower())
    if mapped:
        return mapped

    if "@" in owner_clean:
        return get_employee_name_from_email(owner_clean)

    if owner_clean.islower():
        return " ".join(token.capitalize() for token in owner_clean.split())

    return owner_clean


def normalize_jira_assignee(assignee: object) -> str:
    """Normalize JIRA assignee names into a consistent display format."""

    if assignee is None:
        return DB_NA

    if isinstance(assignee, float) and pd.isna(assignee):
        return DB_NA

    assignee_clean = str(assignee).strip()
    if not assignee_clean or assignee_clean.upper() == "NA":
        return DB_NA

    # Remove stale status tags like "[X]" appended by some sources.
    assignee_clean = re.sub(r"\s*\[X\]\s*$", "", assignee_clean, flags=re.IGNORECASE).strip()
    if not assignee_clean:
        return DB_NA

    if "@" in assignee_clean:
        return get_employee_name_from_email(assignee_clean)

    # Convert "Last, First" into "First Last".
    if "," in assignee_clean:
        last, first = assignee_clean.split(",", 1)
        assignee_clean = f"{first.strip()} {last.strip()}".strip()

    assignee_clean = re.sub(r"\s+", " ", assignee_clean)

    mapped = _JIRA_ASSIGNEE_ALIAS_MAP.get(assignee_clean.lower())
    if mapped:
        return mapped

    if assignee_clean.islower():
        return _smart_title_name(assignee_clean)

    # If the whole string is uppercase, normalize to display case.
    if assignee_clean.upper() == assignee_clean:
        return _smart_title_name(assignee_clean.lower())

    return assignee_clean


_DEFAULT_TEAM_ASSIGNEES = [
    "Brenton Wu",
    "Jonathan Tsao",
    "KJ Fang",
    "Zhiwei He",
    "Frank Lee",
    "Frank Yang",
    "Nicky Chen",
    "Charles Chu",
    "Zhiqiang Cai",
    "Timdaway Lai",
    "Zhanying Gao",
    "Jackx Lee",
    "Lydiax Chien",
    "Johnsonx Su",
    "Xihaox Yang",
    "Henryx Su",
    "Sam Hsu",
    "Bingyue Sun",
    "Bing Chang",
    "Leaweix Chen",
    "Leo Chiang",
    "Steven1 Chen",
    "Wesley Kuo",
    "Tonyx Yeh",
    "Juan Zou",
    "Matt Chen",
    "Yu-Wei Chen",
    "Yu Wei Chen",
]


def _load_team_assignees() -> list[str]:
    """Load team assignees from JSON config with built-in fallback list."""
    _load_env()
    configured_path = _env("CFE_TEAM_MEMBER_FILE", _env("JIRA_TEAM_ASSIGNEES_FILE", ""))
    default_path = Path(__file__).resolve().parent / "cfe_team_member.json"
    legacy_path = Path(__file__).resolve().parent / "jira_team_assignees.json"

    candidate_paths: list[Path] = []
    if configured_path:
        candidate_paths.append(Path(configured_path).expanduser())
    candidate_paths.append(default_path)
    candidate_paths.append(legacy_path)

    for path in candidate_paths:
        if not path.exists():
            continue

        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            log.warning("Failed to read team assignee config %s: %s", path, exc)
            continue

        if isinstance(payload, dict):
            raw_names = payload.get("team_assignees", [])
        else:
            raw_names = payload

        if not isinstance(raw_names, list):
            log.warning("Team assignee config %s is invalid: expected list", path)
            continue

        names = [str(item).strip() for item in raw_names if str(item).strip()]
        if names:
            log.info("Loaded %d team assignees from %s", len(names), path)
            return names

    return list(_DEFAULT_TEAM_ASSIGNEES)


def _build_engineer_name_canonical_map() -> dict[str, str]:
    """Build lower-cased lookup map for team-assignee filtering."""
    mapping: dict[str, str] = {}
    for name in _load_team_assignees():
        key = str(name).strip().lower()
        if key:
            mapping[key] = str(name).strip()
    return mapping


_ENGINEER_NAME_CANONICAL_MAP = _build_engineer_name_canonical_map()


def resolve_engineer_name(
    reporter: object,
    jira_assignee: object,
    jira_external_assignee: object,
    jira_team: object,
) -> str:
    """Resolve engineer from assignee/reporter based on CFE engineer list."""

    reporter_name = normalize_ips_owner_reporter(reporter)
    if reporter_name in (None, "", DB_NA):
        reporter_name = normalize_hsd_owner(str(reporter or ""))
    else:
        hsd_mapped_reporter = normalize_hsd_owner(reporter_name)
        if hsd_mapped_reporter not in (None, "", DB_NA):
            reporter_name = hsd_mapped_reporter

    assignee_name = normalize_jira_assignee(jira_assignee)
    external_assignee_name = normalize_jira_assignee(jira_external_assignee)

    reporter_key = str(reporter_name or "").strip().lower()
    assignee_key = str(assignee_name or "").strip().lower()
    external_assignee_key = str(external_assignee_name or "").strip().lower()
    team_key = str(jira_team or "").strip().lower()

    matt_chen_key = "matt chen"
    is_google_db_team = team_key == "googledb"
    allow_matt_chen = (not is_google_db_team) or (
        assignee_key == matt_chen_key or external_assignee_key == matt_chen_key
    )

    if reporter_key in _ENGINEER_NAME_CANONICAL_MAP:
        resolved = _ENGINEER_NAME_CANONICAL_MAP[reporter_key]
        if resolved == "Matt Chen" and not allow_matt_chen:
            pass
        else:
            return resolved

    if assignee_key in _ENGINEER_NAME_CANONICAL_MAP:
        resolved = _ENGINEER_NAME_CANONICAL_MAP[assignee_key]
        if resolved == "Matt Chen" and not allow_matt_chen:
            pass
        else:
            return resolved

    if external_assignee_key in _ENGINEER_NAME_CANONICAL_MAP:
        resolved = _ENGINEER_NAME_CANONICAL_MAP[external_assignee_key]
        if resolved == "Matt Chen" and not allow_matt_chen:
            pass
        else:
            return resolved

    if reporter_name not in (None, "", DB_NA):
        if reporter_key == matt_chen_key and not allow_matt_chen:
            return DB_NA
        return reporter_name

    return DB_NA


def normalize_hsd_platform(platform: str) -> str:
    """Clean platform labels by dropping trailing platform/family tokens."""

    if not platform or platform == DB_NA:
        return DB_NA

    cleaned = platform.strip()
    if not cleaned:
        return DB_NA

    # Remove trailing descriptors like "platform" or "family"
    while True:
        updated = re.sub(r"\b(platform|family)\b$", "", cleaned, flags=re.IGNORECASE).strip()
        if updated == cleaned:
            break
        cleaned = updated

    # Collapse any double spaces after removals
    cleaned = re.sub(r"\s{2,}", " ", cleaned)

    return cleaned if cleaned else DB_NA


def normalize_hsd_status_reason(value: str) -> str:
    if not value:
        return DB_NA
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() == "na":
        return DB_NA
    lowered = cleaned.lower()
    if "complete" in lowered or "implemented" in lowered or "rejected" in lowered:
        return "closed"
    if "open" in lowered:
        return "open"
    return cleaned


def load_hsd_bugs_from_csv(csv_path: str) -> list[HsdBugData]:
    """Load HSD bugs from a CSV export and map required fields."""

    log.info("Reading HSD CSV data from %s", csv_path)

    try:
        df = pd.read_csv(csv_path, encoding="utf-8")
    except FileNotFoundError:
        log.error("HSD CSV file not found: %s", csv_path)
        return []
    except Exception as exc:
        log.error("Failed to read HSD CSV %s: %s", csv_path, exc)
        return []

    if df.empty:
        log.warning("HSD CSV %s is empty", csv_path)
        return []

    log.info("HSD CSV rows: %d, columns: %d", len(df), len(df.columns))
    log.debug("HSD CSV columns: %s", list(df.columns))

    column_mapping = {
        "hsd_id": ["id", "bug_id", "hsd_id", "article_id", "number"],
        "hsd_promoted_id": ["promoted_id", "promotion_id", "promoted bug", "promoted article id"],
        "hsd_status_reason": ["status_reason", "status reason", "state_reason", "reason"],
        "hsd_customer_detail": ["customer_detail", "customer", "customer detail"],
        "hsd_owner": ["owner", "assigned_to", "assignee"],
        "hsd_title": ["title", "subject", "headline"],
        "hsd_submitted_date": ["submitted_date", "submit_date", "created", "created_date"],
        "hsd_updated_date": ["updated_date", "updated", "modified", "modified_date", "last_modified"],
        "hsd_platform": ["platform", "platform name", "family", "product_family"],
    }

    lower_column_lookup = {str(col).lower(): col for col in df.columns}

    def find_column(field_name: str) -> str | None:
        for candidate in column_mapping.get(field_name, []):
            match = lower_column_lookup.get(candidate.lower())
            if match:
                return match
        return None

    def get_str(row, column_name: str | None) -> str:
        if not column_name:
            return DB_NA
        value = row.get(column_name)
        if pd.isna(value):
            return DB_NA
        value_str = str(value).strip()
        if not value_str or value_str.lower() == "na":
            return DB_NA
        return value_str

    def get_datetime(row, column_name: str | None) -> datetime:
        if not column_name:
            return DB_FUTURE_DATE
        value = row.get(column_name)
        if pd.isna(value):
            return DB_FUTURE_DATE
        parsed = pd.to_datetime(value, errors="coerce", utc=False)
        if pd.isna(parsed):
            return DB_FUTURE_DATE
        if getattr(parsed, "tzinfo", None):
            parsed = parsed.tz_localize(None)
        return parsed.to_pydatetime()

    hsd_bugs: list[HsdBugData] = []

    for _, row in df.iterrows():
        bug = HsdBugData(
            hsd_id=get_str(row, find_column("hsd_id")),
            hsd_promoted_id=get_str(row, find_column("hsd_promoted_id")),
            hsd_status_reason=normalize_hsd_status_reason(get_str(row, find_column("hsd_status_reason"))),
            hsd_customer_detail=get_str(row, find_column("hsd_customer_detail")),
            hsd_owner=normalize_hsd_owner(get_str(row, find_column("hsd_owner"))),
            hsd_title=get_str(row, find_column("hsd_title")),
            hsd_submitted_date=get_datetime(row, find_column("hsd_submitted_date")),
            hsd_updated_date=get_datetime(row, find_column("hsd_updated_date")),
            hsd_platform=normalize_hsd_platform(get_str(row, find_column("hsd_platform"))),
        )
        hsd_bugs.append(bug)

    log.info("Loaded %d HSD bugs from CSV", len(hsd_bugs))
    return hsd_bugs


def fill_mutual_bug_params(_merged_bug_list: list) -> None:
    """
    update merged_bug_list with parameters mutual to both IPS and JIRA bugs
    """

    # get customer data from DB
    _db_manager = DbConnector()
    customer_data = _db_manager.get_customers_data()
    del _db_manager

    # generic flags for all bugs
    for merged_bug in _merged_bug_list:
        # update project (deduced from JIRA or from IPS in case of no JIRA)
        merged_bug.bug_project = get_bug_project(merged_bug)

        # we have JIRA and IPS together
        merged_bug.is_ips_promoted_to_jira = bool(
            merged_bug.jira_data.jira_id != DB_NA and merged_bug.ips_data.ips_case_number
        )

        # get the customer name based on several fields in JIRA and IPS
        merged_bug.customer = get_customer_name(customer_data, merged_bug)
        if merged_bug.hsd_data.hsd_id != DB_NA:
            # Force Microsoft customer label for any HSD-linked issue
            merged_bug.customer = "MICROSOFT"

        # get the reporter - prefer JIRA, if exists
        # technically, if both exist, they should be the same
        if merged_bug.jira_data.jira_reporter_email != DB_NA:
            reporter_email = merged_bug.jira_data.jira_reporter_email
            merged_bug.reporter = get_employee_name_from_email(reporter_email)
        elif merged_bug.ips_data.ips_owner_email != DB_NA:
            reporter_email = merged_bug.ips_data.ips_owner_email
            merged_bug.reporter = get_employee_name_from_email(reporter_email)
        elif merged_bug.ips_data.ips_owner_name != DB_NA:
            merged_bug.reporter = normalize_ips_owner_reporter(merged_bug.ips_data.ips_owner_name)
        elif merged_bug.hsd_data.hsd_owner != DB_NA:
            merged_bug.reporter = normalize_hsd_owner(merged_bug.hsd_data.hsd_owner)
        else:
            merged_bug.reporter = DB_NA

        merged_bug.engineer = resolve_engineer_name(
            merged_bug.reporter,
            merged_bug.jira_data.jira_assignee,
            merged_bug.jira_data.jira_external_assignee,
            merged_bug.jira_data.jira_team,
        )

        # customer closed date
        # take the earlier of the following states, since technically bug is already closed
        # if any of the below happen
        merged_bug.customer_closed_date = min(
            merged_bug.jira_data.jira_closed_date,
            merged_bug.jira_data.jira_implemented_date,
            merged_bug.jira_data.jira_verify_date,
        )


def generate_merged_bug_list(
    _ips_data: list,
    _jira_data: list,
    jira_client: JiraBug,
    hsd_data: list[HsdBugData] | None = None,
) -> list:

    """
    generate a merged list of JIRA and IPS bugs
    """
    _merged_bug_list = []
    if hsd_data is None:
        hsd_data = []

    # join tables - two steps
    # [1] go over all IPS bugs, fix the JIRA links and add them
    #     most of the JIRAs are filed from IPS, meaning, IPS is a larger group
    # [2] go over all JIRA bugs and find those who were not originated by IPS
    #     (there shouldn't be that many)
    # fix ips_jira_id links for all IPS issues having JIRA link
    targeted_jira_cache: dict[str, Optional[JiraBugData]] = {}
    for ips_bug in _ips_data:
        merged_bug = MergedBugData()
        jira_obj = None
        # get the JIRA ID and see if needs to be fixed
        ips_jira_id = ips_bug.ips_jira_id
        if ips_jira_id != DB_NA:
            jira_obj = [_bug for _bug in _jira_data if _bug.jira_id == ips_jira_id]
            if not jira_obj:
                # the JIRA bug listed in IPS was not found in internal dictionary
                # possible reasons:
                # [1] the query for JIRA bugs is incorrect/missing
                #     this case is less common (<1%), but need to review the query and see how it was missed
                # [2] bug was moved to a different project (e.g. was filed as WOT and moved to WIFI)
                #     this case is common (~5%) - need to find the "new" bug and update the DB
                #     the original "old" bug doesn't exist, so we have nothing to do with it
                # [3] bug was deleted from JIRA DB - not sure what we can do here (complain?)
                # search for the bug in JIRA
                actual_bug = jira_client.get_specific_bug(ips_jira_id)
                # check which one of the above happened
                if actual_bug == DB_NA:
                    # bug was not found in JIRA at all, case [3]
                    log.info("JIRA [%s] from IPS [%d] doesn't exist in JIRA DB", ips_jira_id, ips_bug.ips_case_number)
                    # reset the link
                    ips_bug.ips_jira_id = DB_NA
                elif actual_bug == ips_jira_id:
                    # the returned bug is identical to original one, case [1]
                    if actual_bug not in targeted_jira_cache:
                        targeted_jira_cache[actual_bug] = jira_client.get_specific_bug_data(actual_bug)
                    if targeted_jira_cache[actual_bug]:
                        jira_obj = [targeted_jira_cache[actual_bug]]
                        log.info(
                            "JIRA [%s] from IPS [%d] was not found by original query; refreshed by direct issue fetch",
                            actual_bug,
                            ips_bug.ips_case_number,
                        )
                    else:
                        # we 'miss' this bug and will not be able to calc JIRA TAT, since jira_data doesn't include it
                        log.info(
                            "JIRA [%s] from IPS [%d] was not found by original query - check why",
                            actual_bug,
                            ips_bug.ips_case_number,
                        )
                else:
                    # actual_bug != ips_jira_id
                    # the returned bug is different from the original one, case [2]
                    log.info(
                        "IPS [%d] bug changed project, [%s] --> [%s]", ips_bug.ips_case_number, ips_jira_id, actual_bug
                    )
                    # check if the new bug exists in the DB
                    jira_obj = [_bug for _bug in _jira_data if _bug.jira_id == actual_bug]
                    if jira_obj:
                        # exists, good - update new link
                        ips_bug.ips_jira_id = actual_bug
                    else:
                        if actual_bug not in targeted_jira_cache:
                            targeted_jira_cache[actual_bug] = jira_client.get_specific_bug_data(actual_bug)
                        if targeted_jira_cache[actual_bug]:
                            jira_obj = [targeted_jira_cache[actual_bug]]
                            ips_bug.ips_jira_id = actual_bug
                            log.info("JIRA [%s] was not found by original query; refreshed by direct issue fetch", actual_bug)
                        else:
                            log.info("JIRA [%s] was not found by original query - check why", actual_bug)
                            # doesn't exist, case [1]
                            # reset the link
                            ips_bug.ips_jira_id = DB_NA

        # fill the IPS data
        merged_bug.ips_data = ips_bug
        # fill the JIRA data if exist
        # if empty, meaning, no corresponding JIRA bug found for this IPS
        if jira_obj:
            merged_bug.jira_data = jira_obj[0]
            merged_bug.ips_tat_till_jira_hours = get_ips_tat_till_jira(merged_bug)

        # add to final list
        _merged_bug_list.append(merged_bug)

    # go over all JIRA issues and add to list all bugs which exist in JIRA
    # and were not promoted from IPS (were not added previously)
    for jira_bug in _jira_data:
        if not any(_bug for _bug in _merged_bug_list if _bug.jira_data.jira_id == jira_bug.jira_id):
            # bug doesn't exist in merged_bug_list
            log.info("JIRA [%s] was found in query, but was not originated from IPS", jira_bug.jira_id)
            merged_bug = MergedBugData()
            merged_bug.jira_data = jira_bug
            # add to final list
            _merged_bug_list.append(merged_bug)

    if hsd_data:
        jira_lookup = {
            bug.jira_data.jira_id.upper(): bug
            for bug in _merged_bug_list
            if bug.jira_data.jira_id != DB_NA
        }
        ips_lookup = {
            str(bug.ips_data.ips_case_number): bug
            for bug in _merged_bug_list
            if bug.ips_data.ips_case_number
        }

        for hsd_bug in hsd_data:
            attached = False

            promoted_id = hsd_bug.hsd_promoted_id.strip()
            if promoted_id and promoted_id != DB_NA:
                promoted_upper = promoted_id.upper()
                if promoted_upper in jira_lookup:
                    jira_lookup[promoted_upper].hsd_data = hsd_bug
                    attached = True
                elif promoted_id.isdigit():
                    ips_match = ips_lookup.get(str(int(promoted_id)))
                    if ips_match:
                        ips_match.hsd_data = hsd_bug
                        attached = True

            if not attached and hsd_bug.hsd_id != DB_NA:
                hsd_id_upper = hsd_bug.hsd_id.upper()
                jira_match = jira_lookup.get(hsd_id_upper)
                if jira_match:
                    jira_match.hsd_data = hsd_bug
                    attached = True

            if not attached:
                merged_bug = MergedBugData()
                merged_bug.hsd_data = hsd_bug
                _merged_bug_list.append(merged_bug)

    # sanity check, we should not see any JIRA twice
    #bug_list = [_bug.jira_data.jira_id for _bug in _merged_bug_list if _bug.jira_data.jira_id != DB_NA]
    #assert len(bug_list) == len(set(bug_list)), "some JIRA bugs are added twice"

    # update generic/mutual params for both JIRA and IPS
    fill_mutual_bug_params(_merged_bug_list)

    return _merged_bug_list


if __name__ == "__main__":
    log.setLevel(logging.INFO)
    log.info("JIRA bug statistics job starts")

    # parse input params
    parser = argparse.ArgumentParser(description="JIRA bugs stats")
    parser.add_argument("-cy", "--created-year", required=True, type=str, help="filter in bugs created since this year")
    parser.add_argument("--menu", action="store_true", help="force interactive speed test menu")
    parser.add_argument("--no-menu", action="store_true", help="skip interactive speed test menu")
    parser.add_argument(
    "--run-option",
    type=int,
    choices=range(0, 12),
    help="Apply speed-test menu option non-interactively (0-11). Overrides menu.",
)
    parser.add_argument("--hsd-csv", help="Optional path to HSD CSV export for merging")
    parser.add_argument(
        "--backfill-engineer",
        action="store_true",
        help="One-time mode: recompute engineer values in existing ips_jira_bugs table and exit.",
    )
    parser.add_argument(
        "--backfill-engineer-only-missing",
        action="store_true",
        help="When used with --backfill-engineer, only rows with engineer=NA are recomputed.",
    )
    parser.add_argument(
        "--backfill-engineer-dry-run",
        action="store_true",
        help="When used with --backfill-engineer, show how many rows would change without updating DB.",
    )
    parser.add_argument(
        "--allow-ddl",
        action="store_true",
        help="Allow DROP/CREATE table operations. By default only DML is allowed.",
    )

    params = parser.parse_args()

    opt = RunOptions()

    if params.menu:
        opt = pick_run_options_menu()
    elif params.run_option is not None:
        opt = apply_run_option(params.run_option)
    elif not params.no_menu:
        opt = pick_run_options_menu()

    if opt.db_recreate_table and not params.allow_ddl:
        log.warning(
            "DDL safeguard active: forcing append mode (no DROP/CREATE). "
            "Use --allow-ddl to override."
        )
        opt.db_recreate_table = False
    opt.db_auto_add_missing_columns = bool(params.allow_ddl)


    log.info("Run options: %s", opt)

    perf = {}
    t0 = time.perf_counter()
    jira = None
    db_manager = None

    try:
        if params.backfill_engineer:
            with stage_timer("Engineer_backfill", perf):
                db_manager = DbConnector()
                db_manager.backfill_engineer_for_table(
                    table="ips_jira_bugs",
                    only_missing=bool(params.backfill_engineer_only_missing),
                    dry_run=bool(params.backfill_engineer_dry_run),
                )
            total = time.perf_counter() - t0
            log.info("=== Stage timing summary (seconds) ===")
            for k in perf:
                log.info("  %-14s %8.2f", k, perf[k])
            log.info("  %-14s %8.2f", "TOTAL", total)
            log.info("done, total=%s seconds", round(total, 2))
            sys.exit(0)

        # IPS data
        with stage_timer("IPS_fetch", perf):
            ips = IpsBug(params.created_year)
            ips_data = ips.get_all_bugs()
            if opt.limit_ips and opt.limit_ips > 0:
                ips_data = ips_data[: opt.limit_ips]
                log.info("IPS: limiting to first %d rows for testing", opt.limit_ips)

        # JIRA data
        with stage_timer("JIRA_fetch", perf):
            jira = JiraBug(params.created_year, opt)
            jira_data = jira.get_all_bugs()
            if opt.limit_jira and opt.limit_jira > 0:
                jira_data = jira_data[: opt.limit_jira]
                log.info("JIRA: limiting to first %d rows for testing", opt.limit_jira)

        hsd_data: list[HsdBugData] = []
        if params.hsd_csv:
            with stage_timer("HSD_fetch", perf):
                hsd_data = load_hsd_bugs_from_csv(params.hsd_csv)

        # MERGED data (joins IPS + JIRA + HSD)
        with stage_timer("MERGE", perf):
            merged_bug_list = generate_merged_bug_list(ips_data, jira_data, jira, hsd_data)

        # update DB
        if opt.enable_db_insert:
            with stage_timer("Postgres_insert", perf):
                db_manager = DbConnector()
                db_manager.insert_to_table("ips_jira_bugs", merged_bug_list, opt.db_recreate_table, opt=opt)
        else:
            log.info("DB insert disabled by option; skipping Postgres write.")

        total = time.perf_counter() - t0

        log.info("=== Stage timing summary (seconds) ===")
        for k in perf:
            log.info("  %-14s %8.2f", k, perf[k])
        log.info("  %-14s %8.2f", "TOTAL", total)
        log.info("done, total=%s seconds", round(total, 2))
        
    finally:
        # Clean up resources
        if jira is not None:
            try:
                # Close JIRA connection if method exists
                if hasattr(jira, 'close'):
                    jira.close()
                elif hasattr(jira, '_session'):
                    jira._session.close()
                # Try to close the parent class connection
                jira_client = jira.get_jira()
                if jira_client and hasattr(jira_client, 'close'):
                    jira_client.close()
            except Exception as e:
                log.warning("Failed to close JIRA connection: %s", e)
        
        if db_manager is not None:
            try:
                del db_manager
            except Exception as e:
                log.warning("Failed to cleanup DB manager: %s", e)

