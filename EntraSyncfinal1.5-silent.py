# EntraSyncfinal1.5-silent.py
"""
Syncs Entra ID users, devices, groups, shared mailboxes, and Exchange
distribution groups to MySQL.
Silent mode — errors logged to /var/log/entrasync.log, designed for cron.

.env keys:
  DB_HOST, DB_USER, DB_PASSWORD, DB_NAME
  TENANT_ID, CLIENT_ID, CLIENT_SECRET
  TENANT_DOMAIN (or EXO_TENANT_DOMAIN)
  EXO_APP_ID         falls back to CLIENT_ID
  EXO_CERT_FILE      path to .pfx cert
  EXO_CERT_PASSWORD
  EXO_OUTPUT_DIR     defaults to ./exo_output
  EXO_PS_SCRIPT      path to ps1, defaults to ./ExchangeDistrobutiongroups.ps1
  POWERSHELL_BIN     defaults to pwsh
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.users_request_builder import UsersRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_abstractions.headers_collection import HeadersCollection
from dotenv import load_dotenv
import mysql.connector

load_dotenv()

logging.basicConfig(
    filename="/var/log/entrasync.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

change_logger = logging.getLogger("entra.changes")
change_logger.setLevel(logging.INFO)
if not change_logger.handlers:
    _ch = logging.FileHandler("/var/log/entrachanges.log")
    _ch.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    change_logger.addHandler(_ch)
    change_logger.propagate = False

TENANT_ID     = os.getenv("TENANT_ID")
CLIENT_ID     = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
}

EXO_TENANT_DOMAIN = os.getenv("EXO_TENANT_DOMAIN") or os.getenv("TENANT_DOMAIN")
EXO_APP_ID        = os.getenv("EXO_APP_ID")        or os.getenv("CLIENT_ID")
EXO_CERT_FILE     = os.getenv("EXO_CERT_FILE")
EXO_CERT_PASSWORD = os.getenv("EXO_CERT_PASSWORD")
EXO_OUTPUT_DIR    = os.getenv("EXO_OUTPUT_DIR",  "./exo_output")
PS1_PATH          = os.getenv("EXO_PS_SCRIPT",   "./ExchangeDistrobutiongroups.ps1")
POWERSHELL_BIN    = os.getenv("POWERSHELL_BIN",  "pwsh")


def get_client() -> GraphServiceClient:
    credential = ClientSecretCredential(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    return GraphServiceClient(credential)


def get_db_connection():
    conn = mysql.connector.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
    )
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_CONFIG['database']}`")
    cursor.close()
    conn.close()
    return mysql.connector.connect(**DB_CONFIG)


def create_tables(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS entra_users (
            id VARCHAR(36) PRIMARY KEY,
            display_name VARCHAR(255), upn VARCHAR(255),
            first_name VARCHAR(100), last_name VARCHAR(100),
            created_datetime DATETIME, job_title VARCHAR(255),
            department VARCHAR(255), city VARCHAR(100), country VARCHAR(100),
            office_location VARCHAR(255), state VARCHAR(100),
            usage_location VARCHAR(10), account_enabled BOOLEAN,
            licenses TEXT, password_policies VARCHAR(255),
            mobile_phone VARCHAR(50), business_phones VARCHAR(255),
            postal_code VARCHAR(20), street_address VARCHAR(255),
            fax_number VARCHAR(50), last_password_change DATETIME,
            user_type VARCHAR(50), employee_type VARCHAR(100),
            manager_id VARCHAR(36), employee_id VARCHAR(100),
            last_synced DATETIME
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS entra_devices (
            id VARCHAR(36) PRIMARY KEY,
            device_name VARCHAR(255), serial_number VARCHAR(100),
            manufacturer VARCHAR(100), model VARCHAR(100),
            management_agent VARCHAR(100), category VARCHAR(255),
            primary_user_upn VARCHAR(255), primary_user_email VARCHAR(255),
            primary_user_display_name VARCHAR(255), last_synced DATETIME
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS entra_groups (
            id VARCHAR(36) PRIMARY KEY,
            display_name VARCHAR(255), mail_nickname VARCHAR(255),
            mail VARCHAR(255), description TEXT,
            group_types VARCHAR(255), mail_enabled BOOLEAN,
            security_enabled BOOLEAN, visibility VARCHAR(50),
            created_datetime DATETIME, is_assignable_to_role BOOLEAN,
            membership_rule TEXT, membership_rule_processing_state VARCHAR(50),
            proxy_addresses TEXT, on_premises_sync_enabled BOOLEAN,
            on_premises_domain_name VARCHAR(255), has_teams BOOLEAN,
            is_dynamic BOOLEAN, last_synced DATETIME
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shared_mailboxes (
            id VARCHAR(36) PRIMARY KEY,
            display_name VARCHAR(255), primary_email VARCHAR(255),
            forward_to VARCHAR(255), owners TEXT, members TEXT,
            last_synced DATETIME
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS distribution_groups (
            id VARCHAR(255) PRIMARY KEY,
            display_name VARCHAR(255),
            primary_smtp_address VARCHAR(255),
            alias VARCHAR(255),
            name VARCHAR(255),
            group_type VARCHAR(100),
            managed_by TEXT,
            require_sender_authentication BOOLEAN,
            hidden_from_address_lists BOOLEAN,
            member_join_restriction VARCHAR(100),
            member_depart_restriction VARCHAR(100),
            moderation_enabled BOOLEAN,
            send_moderation_notifications VARCHAR(100),
            external_directory_object_id VARCHAR(255),
            last_synced DATETIME
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS distribution_group_members (
            id VARCHAR(255) PRIMARY KEY,
            group_id VARCHAR(255),
            distribution_group_email VARCHAR(255),
            distribution_group_name VARCHAR(255),
            member_display_name VARCHAR(255),
            member_alias VARCHAR(255),
            member_primary_smtp VARCHAR(255),
            member_external_email VARCHAR(255),
            member_name VARCHAR(255),
            member_recipient_type VARCHAR(100),
            member_recipient_type_detail VARCHAR(100),
            member_guid VARCHAR(255),
            member_notes TEXT,
            last_synced DATETIME,
            INDEX idx_dgm_group_id (group_id)
        )
    """)
    conn.commit()
    cursor.close()


def fetch_existing(conn, table, id_col="id"):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(f"SELECT * FROM {table}")
    rows = {str(row[id_col]): row for row in cursor.fetchall()}
    cursor.close()
    return rows


def normalize(val):
    if val is None:
        return ""
    if isinstance(val, bool):
        return "1" if val else "0"
    return str(val)


def log_changes(table, identifier, old_row, new_row, tracked_fields):
    if old_row is None:
        change_logger.info(f"ADDED | {table} | {identifier}")
        return
    diffs = []
    for field in tracked_fields:
        old_val = normalize(old_row.get(field))
        new_val = normalize(new_row.get(field))
        if old_val != new_val:
            diffs.append(f"{field}: {repr(old_val)} -> {repr(new_val)}")
    if diffs:
        change_logger.info(f"CHANGED | {table} | {identifier} | {' | '.join(diffs)}")


def sweep_deleted(conn, table, live_ids, label_col=None):
    cursor = conn.cursor(dictionary=True)
    cols = f"id, {label_col}" if label_col else "id"
    cursor.execute(f"SELECT {cols} FROM {table}")
    rows = cursor.fetchall()
    cursor.close()
    db_ids    = {str(r["id"]) for r in rows}
    label_map = {str(r["id"]): r.get(label_col, r["id"]) for r in rows} if label_col else {}
    removed   = db_ids - live_ids
    if removed:
        cursor = conn.cursor()
        fmt = ",".join(["%s"] * len(removed))
        cursor.execute(f"DELETE FROM {table} WHERE id IN ({fmt})", tuple(removed))
        conn.commit()
        cursor.close()
        for rid in removed:
            change_logger.info(f"DELETED | {table} | {label_map.get(rid, rid)}")


def upsert_user(cursor, user, license_names, manager_id):
    sql = """
        INSERT INTO entra_users (
            id, display_name, upn, first_name, last_name, created_datetime,
            job_title, department, city, country, office_location, state,
            usage_location, account_enabled, licenses, password_policies,
            mobile_phone, business_phones, postal_code, street_address,
            fax_number, last_password_change, user_type, employee_type,
            manager_id, employee_id, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            display_name=VALUES(display_name), upn=VALUES(upn),
            first_name=VALUES(first_name), last_name=VALUES(last_name),
            created_datetime=VALUES(created_datetime), job_title=VALUES(job_title),
            department=VALUES(department), city=VALUES(city), country=VALUES(country),
            office_location=VALUES(office_location), state=VALUES(state),
            usage_location=VALUES(usage_location), account_enabled=VALUES(account_enabled),
            licenses=VALUES(licenses), password_policies=VALUES(password_policies),
            mobile_phone=VALUES(mobile_phone), business_phones=VALUES(business_phones),
            postal_code=VALUES(postal_code), street_address=VALUES(street_address),
            fax_number=VALUES(fax_number), last_password_change=VALUES(last_password_change),
            user_type=VALUES(user_type), employee_type=VALUES(employee_type),
            manager_id=VALUES(manager_id), employee_id=VALUES(employee_id),
            last_synced=VALUES(last_synced)
    """
    phones       = ", ".join(user.business_phones) if user.business_phones else None
    licenses_str = ", ".join(license_names)        if license_names        else None
    cursor.execute(sql, (
        user.id, user.display_name, user.user_principal_name,
        user.given_name, user.surname, user.created_date_time,
        user.job_title, user.department, user.city, user.country,
        user.office_location, user.state, user.usage_location,
        user.account_enabled, licenses_str, user.password_policies,
        user.mobile_phone, phones, user.postal_code, user.street_address,
        user.fax_number, user.last_password_change_date_time, user.user_type,
        getattr(user, "employee_type", None), manager_id,
        getattr(user, "employee_id", None), datetime.now(timezone.utc),
    ))


def upsert_device(cursor, device):
    sql = """
        INSERT INTO entra_devices (
            id, device_name, serial_number, manufacturer, model,
            management_agent, category, primary_user_upn,
            primary_user_email, primary_user_display_name, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            device_name=VALUES(device_name), serial_number=VALUES(serial_number),
            manufacturer=VALUES(manufacturer), model=VALUES(model),
            management_agent=VALUES(management_agent), category=VALUES(category),
            primary_user_upn=VALUES(primary_user_upn),
            primary_user_email=VALUES(primary_user_email),
            primary_user_display_name=VALUES(primary_user_display_name),
            last_synced=VALUES(last_synced)
    """
    cursor.execute(sql, (
        device.id, device.device_name, device.serial_number,
        device.manufacturer, device.model,
        str(device.management_agent.value) if device.management_agent else None,
        device.device_category_display_name, device.user_principal_name,
        device.email_address, device.user_display_name,
        datetime.now(timezone.utc),
    ))


def upsert_group(cursor, group):
    sql = """
        INSERT INTO entra_groups (
            id, display_name, mail_nickname, mail, description,
            group_types, mail_enabled, security_enabled, visibility,
            created_datetime, is_assignable_to_role, membership_rule,
            membership_rule_processing_state, proxy_addresses,
            on_premises_sync_enabled, on_premises_domain_name,
            has_teams, is_dynamic, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            display_name=VALUES(display_name), mail_nickname=VALUES(mail_nickname),
            mail=VALUES(mail), description=VALUES(description),
            group_types=VALUES(group_types), mail_enabled=VALUES(mail_enabled),
            security_enabled=VALUES(security_enabled), visibility=VALUES(visibility),
            created_datetime=VALUES(created_datetime),
            is_assignable_to_role=VALUES(is_assignable_to_role),
            membership_rule=VALUES(membership_rule),
            membership_rule_processing_state=VALUES(membership_rule_processing_state),
            proxy_addresses=VALUES(proxy_addresses),
            on_premises_sync_enabled=VALUES(on_premises_sync_enabled),
            on_premises_domain_name=VALUES(on_premises_domain_name),
            has_teams=VALUES(has_teams), is_dynamic=VALUES(is_dynamic),
            last_synced=VALUES(last_synced)
    """
    group_types  = group.group_types or []
    proxy_str    = ", ".join(group.proxy_addresses) if group.proxy_addresses else None
    resource_opt = getattr(group, "resource_provisioning_options", None) or []
    cursor.execute(sql, (
        group.id, group.display_name, group.mail_nickname, group.mail,
        group.description, ", ".join(group_types) if group_types else None,
        group.mail_enabled, group.security_enabled, group.visibility,
        group.created_date_time, group.is_assignable_to_role, group.membership_rule,
        group.membership_rule_processing_state, proxy_str,
        getattr(group, "on_premises_sync_enabled", None),
        getattr(group, "on_premises_domain_name", None),
        "Team" in resource_opt, "DynamicMembership" in group_types,
        datetime.now(timezone.utc),
    ))


def upsert_mailbox(cursor, mailbox_id, display_name, primary_email,
                   forward_to, owners, members):
    sql = """
        INSERT INTO shared_mailboxes (
            id, display_name, primary_email, forward_to, owners, members, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            display_name=VALUES(display_name), primary_email=VALUES(primary_email),
            forward_to=VALUES(forward_to), owners=VALUES(owners),
            members=VALUES(members), last_synced=VALUES(last_synced)
    """
    cursor.execute(sql, (
        mailbox_id, display_name, primary_email, forward_to,
        ", ".join(owners)  if owners  else None,
        ", ".join(members) if members else None,
        datetime.now(timezone.utc),
    ))


def upsert_distribution_group(cursor, row):
    sql = """
        INSERT INTO distribution_groups (
            id, display_name, primary_smtp_address, alias, name, group_type,
            managed_by, require_sender_authentication, hidden_from_address_lists,
            member_join_restriction, member_depart_restriction, moderation_enabled,
            send_moderation_notifications, external_directory_object_id, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            display_name=VALUES(display_name),
            primary_smtp_address=VALUES(primary_smtp_address),
            alias=VALUES(alias), name=VALUES(name), group_type=VALUES(group_type),
            managed_by=VALUES(managed_by),
            require_sender_authentication=VALUES(require_sender_authentication),
            hidden_from_address_lists=VALUES(hidden_from_address_lists),
            member_join_restriction=VALUES(member_join_restriction),
            member_depart_restriction=VALUES(member_depart_restriction),
            moderation_enabled=VALUES(moderation_enabled),
            send_moderation_notifications=VALUES(send_moderation_notifications),
            external_directory_object_id=VALUES(external_directory_object_id),
            last_synced=VALUES(last_synced)
    """
    cursor.execute(sql, (
        row["group_id"], row.get("display_name"), row.get("primary_smtp_address"),
        row.get("alias"), row.get("name"), row.get("group_type"),
        row.get("managed_by"), row.get("require_sender_authentication"),
        row.get("hidden_from_address_lists"), row.get("member_join_restriction"),
        row.get("member_depart_restriction"), row.get("moderation_enabled"),
        row.get("send_moderation_notifications"),
        row.get("external_directory_object_id"),
        datetime.now(timezone.utc),
    ))


def build_distribution_member_id(row):
    parts = [
        row.get("group_id")              or "",
        row.get("member_guid")           or "",
        row.get("member_primary_smtp")   or "",
        row.get("member_external_email") or "",
        row.get("member_name")           or "",
        row.get("member_display_name")   or "",
        row.get("member_notes")          or "",
    ]
    return "|".join(parts)[:255]


def upsert_distribution_group_member(cursor, row):
    sql = """
        INSERT INTO distribution_group_members (
            id, group_id, distribution_group_email, distribution_group_name,
            member_display_name, member_alias, member_primary_smtp,
            member_external_email, member_name, member_recipient_type,
            member_recipient_type_detail, member_guid, member_notes, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            group_id=VALUES(group_id),
            distribution_group_email=VALUES(distribution_group_email),
            distribution_group_name=VALUES(distribution_group_name),
            member_display_name=VALUES(member_display_name),
            member_alias=VALUES(member_alias),
            member_primary_smtp=VALUES(member_primary_smtp),
            member_external_email=VALUES(member_external_email),
            member_name=VALUES(member_name),
            member_recipient_type=VALUES(member_recipient_type),
            member_recipient_type_detail=VALUES(member_recipient_type_detail),
            member_guid=VALUES(member_guid),
            member_notes=VALUES(member_notes),
            last_synced=VALUES(last_synced)
    """
    cursor.execute(sql, (
        build_distribution_member_id(row),
        row.get("group_id"), row.get("distribution_group_email"),
        row.get("distribution_group_name"), row.get("member_display_name"),
        row.get("member_alias"), row.get("member_primary_smtp"),
        row.get("member_external_email"), row.get("member_name"),
        row.get("member_recipient_type"), row.get("member_recipient_type_detail"),
        row.get("member_guid"), row.get("member_notes"),
        datetime.now(timezone.utc),
    ))


async def sync_users(client, conn, limit=999):
    config = RequestConfiguration()
    config.query_parameters = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
        select=[
            "id", "displayName", "userPrincipalName", "givenName", "surname",
            "createdDateTime", "jobTitle", "department", "city", "country",
            "officeLocation", "state", "usageLocation", "accountEnabled",
            "passwordPolicies", "mobilePhone", "businessPhones", "postalCode",
            "streetAddress", "faxNumber", "lastPasswordChangeDateTime",
            "userType", "employeeType", "manager", "employeeId",
        ],
        top=limit,
        orderby=["displayName"],
    )
    config.headers = HeadersCollection()
    config.headers.add("ConsistencyLevel", "eventual")
    try:
        result = await client.users.get(request_configuration=config)
    except Exception as e:
        logging.critical(f"Failed to fetch users: {e}")
        raise

    existing  = fetch_existing(conn, "entra_users")
    tracked   = [
        "display_name", "upn", "first_name", "last_name", "job_title",
        "department", "city", "country", "office_location", "state",
        "usage_location", "account_enabled", "licenses", "mobile_phone",
        "business_phones", "user_type", "employee_type", "manager_id", "employee_id",
    ]
    entra_ids = set()
    cursor    = conn.cursor()
    while result:
        for user in result.value:
            try:
                entra_ids.add(user.id)
                licenses      = await client.users.by_user_id(user.id).license_details.get()
                license_names = [l.sku_part_number for l in licenses.value]
                manager_id    = None
                try:
                    mgr        = await client.users.by_user_id(user.id).manager.get()
                    manager_id = getattr(mgr, "id", None)
                except Exception:
                    pass
                phones  = ", ".join(user.business_phones) if user.business_phones else None
                new_row = {
                    "display_name": user.display_name,
                    "upn": user.user_principal_name,
                    "first_name": user.given_name, "last_name": user.surname,
                    "job_title": user.job_title, "department": user.department,
                    "city": user.city, "country": user.country,
                    "office_location": user.office_location, "state": user.state,
                    "usage_location": user.usage_location,
                    "account_enabled": user.account_enabled,
                    "licenses": ", ".join(license_names) if license_names else None,
                    "mobile_phone": user.mobile_phone, "business_phones": phones,
                    "user_type": user.user_type,
                    "employee_type": getattr(user, "employee_type", None),
                    "manager_id": manager_id,
                    "employee_id": getattr(user, "employee_id", None),
                }
                log_changes("entra_users", user.user_principal_name,
                            existing.get(user.id), new_row, tracked)
                upsert_user(cursor, user, license_names, manager_id)
            except Exception as e:
                logging.warning(f"Skipped user {getattr(user, 'user_principal_name', 'unknown')}: {e}")
        if result.odata_next_link:
            result = await client.users.with_url(result.odata_next_link).get()
        else:
            break
    conn.commit()
    cursor.close()
    sweep_deleted(conn, "entra_users", entra_ids, label_col="upn")


async def sync_devices(client, conn, limit=999):
    from msgraph.generated.device_management.managed_devices.managed_devices_request_builder import ManagedDevicesRequestBuilder
    config = RequestConfiguration()
    config.query_parameters = ManagedDevicesRequestBuilder.ManagedDevicesRequestBuilderGetQueryParameters(
        select=[
            "id", "deviceName", "serialNumber", "manufacturer", "model",
            "managementAgent", "deviceCategoryDisplayName", "userPrincipalName",
            "emailAddress", "userDisplayName",
        ],
        top=limit,
    )
    try:
        result = await client.device_management.managed_devices.get(request_configuration=config)
    except Exception as e:
        logging.critical(f"Failed to fetch devices: {e}")
        raise

    existing  = fetch_existing(conn, "entra_devices")
    tracked   = [
        "device_name", "serial_number", "manufacturer", "model",
        "management_agent", "category", "primary_user_upn",
        "primary_user_email", "primary_user_display_name",
    ]
    entra_ids = set()
    cursor    = conn.cursor()
    while result:
        for device in result.value:
            try:
                entra_ids.add(device.id)
                new_row = {
                    "device_name": device.device_name,
                    "serial_number": device.serial_number,
                    "manufacturer": device.manufacturer, "model": device.model,
                    "management_agent": str(device.management_agent.value) if device.management_agent else None,
                    "category": device.device_category_display_name,
                    "primary_user_upn": device.user_principal_name,
                    "primary_user_email": device.email_address,
                    "primary_user_display_name": device.user_display_name,
                }
                log_changes("entra_devices", device.device_name,
                            existing.get(device.id), new_row, tracked)
                upsert_device(cursor, device)
            except Exception as e:
                logging.warning(f"Skipped device {getattr(device, 'device_name', 'unknown')}: {e}")
        if result.odata_next_link:
            result = await client.device_management.managed_devices.with_url(result.odata_next_link).get()
        else:
            break
    conn.commit()
    cursor.close()
    sweep_deleted(conn, "entra_devices", entra_ids, label_col="device_name")


async def sync_groups(client, conn, limit=999):
    from msgraph.generated.groups.groups_request_builder import GroupsRequestBuilder
    config = RequestConfiguration()
    config.query_parameters = GroupsRequestBuilder.GroupsRequestBuilderGetQueryParameters(
        select=[
            "id", "displayName", "mailNickname", "mail", "description",
            "groupTypes", "mailEnabled", "securityEnabled", "visibility",
            "createdDateTime", "isAssignableToRole", "membershipRule",
            "membershipRuleProcessingState", "proxyAddresses",
            "onPremisesSyncEnabled", "onPremisesDomainName",
            "resourceProvisioningOptions",
        ],
        top=limit,
    )
    config.headers = HeadersCollection()
    config.headers.add("ConsistencyLevel", "eventual")
    try:
        result = await client.groups.get(request_configuration=config)
    except Exception as e:
        logging.critical(f"Failed to fetch groups: {e}")
        raise

    existing  = fetch_existing(conn, "entra_groups")
    tracked   = [
        "display_name", "mail", "description", "group_types",
        "mail_enabled", "security_enabled", "visibility", "membership_rule",
        "has_teams", "is_dynamic", "on_premises_sync_enabled", "on_premises_domain_name",
    ]
    entra_ids = set()
    cursor    = conn.cursor()
    while result:
        for group in result.value:
            try:
                entra_ids.add(group.id)
                group_types  = group.group_types or []
                resource_opt = getattr(group, "resource_provisioning_options", None) or []
                new_row = {
                    "display_name": group.display_name, "mail": group.mail,
                    "description": group.description,
                    "group_types": ", ".join(group_types) if group_types else None,
                    "mail_enabled": group.mail_enabled,
                    "security_enabled": group.security_enabled,
                    "visibility": group.visibility,
                    "membership_rule": group.membership_rule,
                    "has_teams": "Team" in resource_opt,
                    "is_dynamic": "DynamicMembership" in group_types,
                    "on_premises_sync_enabled": getattr(group, "on_premises_sync_enabled", None),
                    "on_premises_domain_name":  getattr(group, "on_premises_domain_name", None),
                }
                log_changes("entra_groups", group.display_name,
                            existing.get(group.id), new_row, tracked)
                upsert_group(cursor, group)
            except Exception as e:
                logging.warning(f"Skipped group {getattr(group, 'display_name', 'unknown')}: {e}")
        if result.odata_next_link:
            result = await client.groups.with_url(result.odata_next_link).get()
        else:
            break
    conn.commit()
    cursor.close()
    sweep_deleted(conn, "entra_groups", entra_ids, label_col="display_name")


async def get_group_members(client, group_id):
    try:
        result = await client.groups.by_group_id(group_id).members.get()
        return [m.display_name for m in result.value if m.display_name]
    except Exception:
        return []


async def get_group_owners(client, group_id):
    try:
        result = await client.groups.by_group_id(group_id).owners.get()
        return [o.display_name for o in result.value if o.display_name]
    except Exception:
        return []


async def sync_shared_mailboxes(client, conn, limit=999):
    config = RequestConfiguration()
    config.query_parameters = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
        select=["id", "displayName", "mail"],
        top=limit,
    )
    config.headers = HeadersCollection()
    config.headers.add("ConsistencyLevel", "eventual")
    try:
        result = await client.users.get(request_configuration=config)
    except Exception as e:
        logging.critical(f"Failed to fetch users for mailbox sync: {e}")
        raise

    if not result or not result.value:
        return

    try:
        groups_result = await client.groups.get()
    except Exception as e:
        logging.critical(f"Failed to fetch groups for mailbox sync: {e}")
        raise

    groups_by_email = {}
    for g in (groups_result.value or []):
        if g.mail:
            groups_by_email[g.mail.lower()] = g.id

    existing  = fetch_existing(conn, "shared_mailboxes")
    tracked   = ["display_name", "primary_email", "forward_to", "owners", "members"]
    entra_ids = set()
    cursor    = conn.cursor()
    for user in result.value:
        if not user.mail:
            continue
        try:
            settings     = await client.users.by_user_id(user.id).mailbox_settings.get()
            user_purpose = settings.user_purpose
            if not user_purpose or user_purpose.value.lower() != "shared":
                continue
            forward_to = getattr(settings, "forwarding_smtp_address", None)
        except Exception as e:
            logging.warning(f"Skipped mailbox {user.mail}: {e}")
            continue
        entra_ids.add(user.id)
        owners, members = [], []
        group_id = groups_by_email.get(user.mail.lower())
        if group_id:
            owners  = await get_group_owners(client, group_id)
            members = await get_group_members(client, group_id)
        new_row = {
            "display_name": user.display_name, "primary_email": user.mail,
            "forward_to": forward_to,
            "owners":  ", ".join(owners)  if owners  else None,
            "members": ", ".join(members) if members else None,
        }
        log_changes("shared_mailboxes", user.mail,
                    existing.get(user.id), new_row, tracked)
        upsert_mailbox(cursor, user.id, user.display_name, user.mail,
                       forward_to, owners, members)
    conn.commit()
    cursor.close()
    sweep_deleted(conn, "shared_mailboxes", entra_ids, label_col="primary_email")


def run_distribution_group_export():
    missing = [k for k, v in {
        "EXO_TENANT_DOMAIN/TENANT_DOMAIN": EXO_TENANT_DOMAIN,
        "EXO_APP_ID/CLIENT_ID":            EXO_APP_ID,
        "EXO_CERT_FILE":                   EXO_CERT_FILE,
        "EXO_CERT_PASSWORD":               EXO_CERT_PASSWORD,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing .env keys: {', '.join(missing)}")

    ps1 = Path(PS1_PATH)
    if not ps1.exists():
        raise RuntimeError(
            f"PowerShell script not found: {ps1}  "
            "Set EXO_PS_SCRIPT in .env to the full path of ExchangeDistrobutiongroups.ps1"
        )

    output_dir = Path(EXO_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        POWERSHELL_BIN, str(ps1),
        "-TenantDomain",        EXO_TENANT_DOMAIN,
        "-AppId",               EXO_APP_ID,
        "-CertificateFilePath", EXO_CERT_FILE,
        "-CertificatePassword", EXO_CERT_PASSWORD,
        "-OutputFolder",        str(output_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"Distribution group export failed:\n{result.stderr or result.stdout}")
        raise RuntimeError("Distribution group PowerShell export failed")
    return output_dir


def sync_distribution_groups(conn):
    output_dir   = run_distribution_group_export()
    groups_path  = output_dir / "distribution_groups.json"
    members_path = output_dir / "distribution_group_members.json"
    if not groups_path.exists() or not members_path.exists():
        raise RuntimeError("Distribution group export JSON files were not created")

    groups_data  = json.loads(groups_path.read_text(encoding="utf-8")  or "[]")
    members_data = json.loads(members_path.read_text(encoding="utf-8") or "[]")

    if isinstance(groups_data,  dict): groups_data  = [groups_data]
    if isinstance(members_data, dict): members_data = [members_data]

    existing_groups  = fetch_existing(conn, "distribution_groups")
    existing_members = fetch_existing(conn, "distribution_group_members")

    group_tracked = [
        "display_name", "primary_smtp_address", "alias", "name", "group_type",
        "managed_by", "require_sender_authentication", "hidden_from_address_lists",
        "member_join_restriction", "member_depart_restriction", "moderation_enabled",
        "send_moderation_notifications", "external_directory_object_id",
    ]
    member_tracked = [
        "group_id", "distribution_group_email", "distribution_group_name",
        "member_display_name", "member_alias", "member_primary_smtp",
        "member_external_email", "member_name", "member_recipient_type",
        "member_recipient_type_detail", "member_guid", "member_notes",
    ]

    cursor     = conn.cursor()
    group_ids  = set()
    member_ids = set()

    for row in groups_data:
        group_id = str(row.get("group_id") or "")
        if not group_id:
            continue
        group_ids.add(group_id)
        new_row = {k: row.get(k) for k in group_tracked}
        log_changes("distribution_groups",
                    row.get("primary_smtp_address") or group_id,
                    existing_groups.get(group_id), new_row, group_tracked)
        upsert_distribution_group(cursor, row)

    for row in members_data:
        member_id = build_distribution_member_id(row)
        member_ids.add(member_id)
        new_row = {k: row.get(k) for k in member_tracked}
        label = (
            f"{row.get('distribution_group_email')} -> "
            f"{row.get('member_primary_smtp') or row.get('member_external_email') or row.get('member_display_name') or member_id}"
        )
        log_changes("distribution_group_members", label,
                    existing_members.get(member_id), new_row, member_tracked)
        upsert_distribution_group_member(cursor, row)

    conn.commit()
    cursor.close()
    sweep_deleted(conn, "distribution_groups",        group_ids,  label_col="primary_smtp_address")
    sweep_deleted(conn, "distribution_group_members", member_ids, label_col="distribution_group_email")


async def main():
    try:
        client = get_client()
        conn   = get_db_connection()
        create_tables(conn)
        await sync_users(client, conn)
        await sync_devices(client, conn)
        await sync_groups(client, conn)
        await sync_shared_mailboxes(client, conn)
        sync_distribution_groups(conn)
        conn.close()
    except Exception as e:
        logging.critical(f"Sync aborted: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())