"""Credentials and settings for various WCD Scripts"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


def _load_env() -> None:
    if load_dotenv is None:
        return
    base_dir = Path(__file__).resolve().parents[1]
    candidates = [
        base_dir / ".env",
        base_dir / "jira_customer_tat" / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
            break


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip() or default


_load_env()


class JobNames:
    """Names of Jenkins jobs"""

    nightly = "windows-wifi-driver/Trigger-Nightly"
    bsod_parser = "windows-wifi-driver/BSOD_PARSER"
    skynet = "windows-wifi-driver/SKYNET"
    skynet_node_placeholder = "windows-wifi-driver/SKYNET_NODE_PLACEHOLDER"
    lab_tests_runner = "windows-wifi-driver/Skynet_Lab_tests_runner"
    zorro = "Zorro"



class Artifactory:
    """Artifactory settings and credentials"""

    server = "https://ubit-artifactory-il.intel.com/artifactory/"
    username = _env("ARTIFACTORY_USER")

class PostgresCustomerEngineeringDb:
    """wireless customer engineering group database"""

    database = _env("DB_NAME")
    user = _env("DB_USER")
    password = _env("DB_PASS")
    host = _env("DB_HOST")
    port = _env("DB_PORT", "5433")

class DBaaS:
    """Database As A Service"""

    username = _env("DBAAS_USERNAME")
    instance_url = "sql1312-lc-in.ger.corp.intel.com"
    instance_port = "3181"


class WinDRVJenkins:
    """Settings and credentials for Jenkins Server"""

    @staticmethod
    def get_instance_from_url(url: str) -> str:
        """Get the Jenkins instance settings from the URL"""
        for instance in WinDRVJenkins.__dict__.values():
            if hasattr(instance, "url") and url.startswith(instance.url):
                return instance
        raise ValueError(f"Unknown Jenkins instance for URL: {url}")

    class Legacy:
        """Settings and credentials for the old Jenkins (as sys_windrvbuild)"""

        name = "Legacy"
        url = "https://cbjenkins-il.devtools.intel.com/teams-windows-wifi-driver/"
        username = _env("JENKINS_LEGACY_USERNAME")
        #_sys_windrvbuild.username
        token = _env("JENKINS_LEGACY_TOKEN")

    class Pre:
        """Settings and credentials for the Pre Jenkins (as sys_windrvbuild)"""

        name = "Pre"
        url = "https://cje-il-prod01.devtools.intel.com/ccg-cps-wifiwindrvpre/"
        username = _env("JENKINS_PRE_USERNAME")
        #_sys_windrvbuild.username
        token = _env("JENKINS_PRE_TOKEN")

    class Prod:
        """Settings and credentials for the Production Jenkins (as sys_windrvbuild)"""

        name = "Prod"
        url = "https://cje-il-prod01.devtools.intel.com/ccg-cps-wifiwindrvprod/"
        username = _env("JENKINS_PROD_USERNAME")
        #_sys_windrvbuild.username
        token = _env("JENKINS_PROD_TOKEN")


class GitRepoUrls:
    """Settings GIT repo's Urls"""

    drv = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-dev"
    fw = "ssh://gerritwcs.ir.intel.com:29418/wcd_fw-dev"
    wapi = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-wapi"
    usc = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-usc"
    sandbox = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-sandbox"
    msi_installer = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-msi_installer"
    eFireman = "ssh://gerritwcs.ir.intel.com:29418/iwlwifi_efireman"
    pytm = "ssh://gerritwcs.ir.intel.com:29418/titan-pytm-tests"
    build_dev = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-build_dev"
    nightly_test_config = "ssh://gerritwcs.ir.intel.com:29418/wifi_drv-nightly_test_config"


class Jira:
    """Settings and credentials for the migrated Jira Cloud connector."""

    server = _env("JIRA_CLOUD_SERVER")
    staging_server = _env("JIRA_CLOUD_STAGING_SERVER")
    test_server = _env("JIRA_CLOUD_TEST_SERVER")
    username = _env("JIRA_USER")
    password = _env("JIRA_PASSWORD")
    certificate = _env("JIRA_CERTIFICATE", "NA")



class Shares:
    """Various network share paths"""

    zip_listener = "//infs089b.iil.intel.com/Zip_Listener/buildSystem/"
    zip_listener_attestation = "//infs089b.iil.intel.com/Zip_Listener/buildSystem/attestation"
    jer_storatge = "//jeswcd13.ger.corp.intel.com/wcd_driver_storage"
    dfs = "//ger.corp.intel.com/ec/proj/ha/WCS/PotatoFarm/WinDRV/"
    reports_share = "//ger.corp.intel.com/ec/proj/ha/WCS/WCD_Win_DRV_Reports"
    db_backups = "//ger.corp.intel.com/ec/proj/ha/WCS/DBBackup"
    ut_layout = "//infs089b.iil.intel.com/WHCK/UT_BUILD"
    esl_integ_ut_layout = "//infs089b.iil.intel.com/WHCK/ESL_INTEG"
    bsod_parser_perci = "//ger.corp.intel.com/ec/proj/ha/WCS/PotatoFarm/BSOD_Parser/PerCI_Samples"


class Ips:
    """Intel Pre-Sales (IPS) DB for customer issues"""

    user = _env("SNOWFLAKE_IPS_USER")
    password = _env("SNOWFLAKE_IPS_PASSWORD")
    role = _env("SNOWFLAKE_IPS_ROLE")
    account = _env("SNOWFLAKE_IPS_ACCOUNT")
    warehouse = _env("SNOWFLAKE_IPS_WAREHOUSE")
    database = _env("SNOWFLAKE_IPS_DATABASE")

