#EntraSyncfinal1.3-silent.py

"""
EntraSyncfinal1.3-Silent.py
Syncs Entra ID users, devices, groups, and shared mailboxes to MySQL.
Silent mode — errors logged to /var/log/entrasync.log, designed for cron.

Changes in 1.3:
- All sync functions now delete DB rows no longer present in Entra (sweep logic)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.users_request_builder import UsersRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_abstractions.headers_collection import HeadersCollection
from dotenv import load_dotenv
import mysql.connector

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename="/var/log/entrasync.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ── Credentials ───────────────────────────────────────────────────────────────
TENANT_ID     = os.getenv("TENANT_ID")
CLIENT_ID     = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
}

# ── Graph Client ──────────────────────────────────────────────────────────────
def get_client() -> GraphServiceClient:
    credential = ClientSecretCredential(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    return GraphServiceClient(credential)

# ── MySQL Helpers ─────────────────────────────────────────────────────────────
def get_db_connection():
    conn = mysql.connector.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
    )
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']}")
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
    conn.commit()
    cursor.close()

# ── Sweep Helper ──────────────────────────────────────────────────────────────
def sweep_deleted(conn, table, entra_ids):
    """Delete rows from table whose IDs are no longer in Entra."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT id FROM {table}")
    db_ids = {row[0] for row in cursor.fetchall()}
    removed = db_ids - entra_ids
    if removed:
        fmt = ",".join(["%s"] * len(removed))
        cursor.execute(f"DELETE FROM {table} WHERE id IN ({fmt})", tuple(removed))
        logging.error(f"Sweep: deleted {len(removed)} removed records from {table}: {removed}")
    conn.commit()
    cursor.close()

# ── Upsert Helpers ────────────────────────────────────────────────────────────
def upsert_user(cursor, user, license_names, manager_id):
    sql = """
        INSERT INTO entra_users (
            id, display_name, upn, first_name, last_name, created_datetime,
            job_title, department, city, country, office_location, state,
            usage_location, account_enabled, licenses, password_policies,
            mobile_phone, business_phones, postal_code, street_address,
            fax_number, last_password_change,
            user_type, employee_type, manager_id, employee_id,
            last_synced
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
    phones = ", ".join(user.business_phones) if user.business_phones else None
    licenses_str = ", ".join(license_names) if license_names else None
    now = datetime.now(timezone.utc)
    cursor.execute(sql, (
        user.id, user.display_name, user.user_principal_name,
        user.given_name, user.surname, user.created_date_time,
        user.job_title, user.department, user.city, user.country,
        user.office_location, user.state, user.usage_location,
        user.account_enabled, licenses_str, user.password_policies,
        user.mobile_phone, phones, user.postal_code, user.street_address,
        user.fax_number, user.last_password_change_date_time,
        user.user_type,
        getattr(user, "employee_type", None),
        manager_id,
        getattr(user, "employee_id", None),
        now,
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
    now = datetime.now(timezone.utc)
    cursor.execute(sql, (
        device.id, device.device_name, device.serial_number,
        device.manufacturer, device.model,
        str(device.management_agent.value) if device.management_agent else None,
        device.device_category_display_name, device.user_principal_name,
        device.email_address, device.user_display_name, now,
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
    now = datetime.now(timezone.utc)
    group_types = group.group_types or []
    group_types_str = ", ".join(group_types) if group_types else None
    proxy_str = ", ".join(group.proxy_addresses) if group.proxy_addresses else None
    resource_options = getattr(group, "resource_provisioning_options", None) or []
    has_teams = "Team" in resource_options
    is_dynamic = "DynamicMembership" in group_types
    cursor.execute(sql, (
        group.id, group.display_name, group.mail_nickname, group.mail,
        group.description, group_types_str, group.mail_enabled,
        group.security_enabled, group.visibility, group.created_date_time,
        group.is_assignable_to_role, group.membership_rule,
        group.membership_rule_processing_state, proxy_str,
        getattr(group, "on_premises_sync_enabled", None),
        getattr(group, "on_premises_domain_name", None),
        has_teams, is_dynamic, now,
    ))

def upsert_mailbox(cursor, mailbox_id, display_name, primary_email,
                   forward_to, owners, members):
    sql = """
        INSERT INTO shared_mailboxes (
            id, display_name, primary_email, forward_to,
            owners, members, last_synced
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
        display_name=VALUES(display_name),
        primary_email=VALUES(primary_email),
        forward_to=VALUES(forward_to),
        owners=VALUES(owners),
        members=VALUES(members),
        last_synced=VALUES(last_synced)
    """
    cursor.execute(sql, (
        mailbox_id, display_name, primary_email, forward_to,
        ", ".join(owners) if owners else None,
        ", ".join(members) if members else None,
        datetime.now(timezone.utc),
    ))

# ── Graph Sync Functions ──────────────────────────────────────────────────────
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
        logging.critical(f"Failed to fetch users from Graph API: {e}")
        raise

    entra_ids = set()
    cursor = conn.cursor()
    while result:
        for user in result.value:
            try:
                entra_ids.add(user.id)
                licenses = await client.users.by_user_id(user.id).license_details.get()
                license_names = [l.sku_part_number for l in licenses.value]
                manager_id = None
                try:
                    mgr = await client.users.by_user_id(user.id).manager.get()
                    manager_id = getattr(mgr, "id", None)
                except Exception:
                    pass
                upsert_user(cursor, user, license_names, manager_id)
            except mysql.connector.Error as e:
                logging.error(f"DB error on user {user.user_principal_name}: {e}")
                raise
            except Exception as e:
                logging.warning(f"Skipped user {user.user_principal_name}: {e}")
        try:
            if result.odata_next_link:
                result = await client.users.with_url(result.odata_next_link).get()
            else:
                break
        except Exception as e:
            logging.critical(f"Fatal error fetching next user page: {e}")
            raise

    conn.commit()
    cursor.close()

    # Sweep deleted users
    sweep_deleted(conn, "entra_users", entra_ids)

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
        logging.critical(f"Failed to fetch devices from Graph API: {e}")
        raise

    entra_ids = set()
    cursor = conn.cursor()
    while result:
        for device in result.value:
            try:
                entra_ids.add(device.id)
                upsert_device(cursor, device)
            except mysql.connector.Error as e:
                logging.error(f"DB error on device {device.device_name}: {e}")
                raise
            except Exception as e:
                logging.warning(f"Skipped device {device.device_name}: {e}")
        try:
            if result.odata_next_link:
                result = await client.device_management.managed_devices.with_url(result.odata_next_link).get()
            else:
                break
        except Exception as e:
            logging.critical(f"Fatal error fetching next device page: {e}")
            raise

    conn.commit()
    cursor.close()

    # Sweep deleted devices
    sweep_deleted(conn, "entra_devices", entra_ids)

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
        logging.critical(f"Failed to fetch groups from Graph API: {e}")
        raise

    entra_ids = set()
    cursor = conn.cursor()
    while result:
        for group in result.value:
            try:
                entra_ids.add(group.id)
                upsert_group(cursor, group)
            except mysql.connector.Error as e:
                logging.error(f"DB error on group {group.display_name}: {e}")
                raise
            except Exception as e:
                logging.warning(f"Skipped group {group.display_name}: {e}")
        try:
            if result.odata_next_link:
                result = await client.groups.with_url(result.odata_next_link).get()
            else:
                break
        except Exception as e:
            logging.critical(f"Fatal error fetching next group page: {e}")
            raise

    conn.commit()
    cursor.close()

    # Sweep deleted groups
    sweep_deleted(conn, "entra_groups", entra_ids)

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

    entra_ids = set()
    cursor = conn.cursor()
    for user in result.value:
        if not user.mail:
            continue
        try:
            settings = await client.users.by_user_id(user.id).mailbox_settings.get()
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

        try:
            upsert_mailbox(cursor, user.id, user.display_name, user.mail,
                           forward_to, owners, members)
        except mysql.connector.Error as e:
            logging.error(f"DB error on mailbox {user.mail}: {e}")
            raise

    conn.commit()
    cursor.close()

    # Sweep deleted shared mailboxes
    sweep_deleted(conn, "shared_mailboxes", entra_ids)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    try:
        client = get_client()
        conn = get_db_connection()
        create_tables(conn)
        await sync_users(client, conn)
        await sync_devices(client, conn)
        await sync_groups(client, conn)
        await sync_shared_mailboxes(client, conn)
        conn.close()
    except Exception as e:
        logging.critical(f"Sync aborted: {e}")
        raise SystemExit(1)

if __name__ == "__main__":
    asyncio.run(main())