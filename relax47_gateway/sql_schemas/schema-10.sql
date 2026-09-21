-- RELAX47 schema 10: empty database only. Upgrade existing databases with SQLiteRepository.migrate(10).
PRAGMA foreign_keys=ON;
BEGIN TRANSACTION;
CREATE TABLE access_decisions (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, stay_id TEXT NOT NULL, zone_id TEXT NOT NULL, evaluated_for TEXT NOT NULL, allowed INTEGER NOT NULL CHECK(allowed IN (0,1)), reason TEXT NOT NULL, evaluated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE access_policies (property_id TEXT NOT NULL, zone_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), mode TEXT NOT NULL CHECK(mode IN ('stay_window','always','deny')), requires_pass INTEGER NOT NULL CHECK(requires_pass IN (0,1)), entry_offset_seconds INTEGER NOT NULL DEFAULT 0, exit_offset_seconds INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,zone_id));
CREATE TABLE audit_changes (
        event_id TEXT PRIMARY KEY REFERENCES event_context(event_id), target_type TEXT NOT NULL,
        target_id TEXT NOT NULL, before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
        after_json TEXT CHECK(after_json IS NULL OR json_valid(after_json)), result TEXT NOT NULL);
CREATE TABLE billing_accounts (stay_id TEXT PRIMARY KEY REFERENCES stays(id), property_id TEXT NOT NULL, current_quote_id TEXT NOT NULL UNIQUE REFERENCES billing_quotes(id), version INTEGER NOT NULL CHECK(version>0));
CREATE TABLE billing_quotes (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, stay_id TEXT NOT NULL REFERENCES stays(id), stay_version INTEGER NOT NULL, tariff_id TEXT NOT NULL, tariff_version INTEGER NOT NULL, payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(property_id,tariff_id,tariff_version) REFERENCES rate_plans(property_id,id,version));
CREATE TABLE booking_commands (property_id TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(property_id,actor,request_id));
CREATE TABLE bs2_commands (property_id TEXT NOT NULL, command_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('create_pass','delete_pass')), fingerprint TEXT NOT NULL, command_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('unknown','accepted','confirmed')), job_id TEXT, receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,command_id), UNIQUE(property_id,job_id));
CREATE TABLE configuration_revisions (
        property_id TEXT NOT NULL REFERENCES properties(id), namespace TEXT NOT NULL, item_key TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), actor TEXT NOT NULL, created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,namespace,item_key,revision));
CREATE TABLE device_imports (property_id TEXT NOT NULL REFERENCES properties(id), source_sha256 TEXT NOT NULL, actor TEXT NOT NULL, imported_at TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(property_id,source_sha256));
CREATE TABLE event_context (
        event_id TEXT PRIMARY KEY REFERENCES events(event_id),
        property_id TEXT NOT NULL REFERENCES properties(id), category TEXT NOT NULL,
        severity TEXT NOT NULL CHECK(severity IN ('debug','info','warning','error','critical')),
        stay_id TEXT, vehicle_id TEXT, pass_id TEXT, zone_id TEXT, sensor_entity_id TEXT,
        parent_event_id TEXT, received_at TEXT NOT NULL,
        UNIQUE(property_id,event_id),
        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),
        FOREIGN KEY(property_id,vehicle_id) REFERENCES vehicles(property_id,id),
        FOREIGN KEY(property_id,pass_id) REFERENCES passes(property_id,id),
        FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id),
        FOREIGN KEY(property_id,parent_event_id) REFERENCES event_context(property_id,event_id));
CREATE TABLE event_media (
        property_id TEXT NOT NULL, event_id TEXT NOT NULL, media_id TEXT NOT NULL, role TEXT NOT NULL,
        PRIMARY KEY(property_id,event_id,media_id,role),
        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id),
        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));
CREATE TABLE event_origins (
        property_id TEXT NOT NULL, source TEXT NOT NULL, source_event_id TEXT NOT NULL,
        event_id TEXT NOT NULL, fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
        PRIMARY KEY(property_id,source,source_event_id),
        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));
CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
CREATE TABLE gate_event_links (
        property_id TEXT NOT NULL, gate_event_id TEXT NOT NULL, event_id TEXT NOT NULL,
        PRIMARY KEY(property_id,gate_event_id), UNIQUE(property_id,event_id),
        FOREIGN KEY(property_id,gate_event_id) REFERENCES gate_events(property_id,id),
        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));
CREATE TABLE gate_events (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, vehicle_id TEXT NOT NULL REFERENCES vehicles(id), stay_id TEXT REFERENCES stays(id), pass_id TEXT REFERENCES passes(id), direction TEXT NOT NULL CHECK(direction IN ('entry','exit')), occurred_at TEXT NOT NULL, source TEXT NOT NULL, source_event_id TEXT, payload_json TEXT NOT NULL, UNIQUE(property_id,source,source_event_id));
CREATE TABLE guests (
        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version>0), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,id));
CREATE TABLE incidents (
            incident_id TEXT PRIMARY KEY,
            error_code TEXT NOT NULL,
            component TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            probable_cause TEXT NOT NULL DEFAULT '',
            recommended_action TEXT NOT NULL DEFAULT ''
        , property_id TEXT NOT NULL DEFAULT '', device_id TEXT, zone_id TEXT, details_json TEXT NOT NULL DEFAULT '{}', automatic_action TEXT NOT NULL DEFAULT '');
CREATE TABLE integration_circuits (property_id TEXT NOT NULL, component TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('closed','open','half_open')), failure_count INTEGER NOT NULL DEFAULT 0, open_until TEXT, version INTEGER NOT NULL CHECK(version>0), updated_at TEXT NOT NULL, PRIMARY KEY(property_id,component));
CREATE TABLE logical_devices (property_id TEXT NOT NULL, id TEXT NOT NULL, zone_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), compatibility_entity_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,id), UNIQUE(property_id,compatibility_entity_id), FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id));
CREATE TABLE media_assets (
        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,
        mime_type TEXT NOT NULL, sha256 TEXT CHECK(sha256 IS NULL OR length(sha256)=64),
        size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes>=0), captured_at TEXT,
        storage_kind TEXT NOT NULL CHECK(storage_kind IN ('blob','file','object')),
        content BLOB, storage_uri TEXT, verified_at TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','available','missing','damaged')),
        retain_until TEXT, preserve INTEGER NOT NULL DEFAULT 1 CHECK(preserve IN (0,1)),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(property_id,id),
        UNIQUE(property_id,sha256),
        CHECK((storage_kind='blob' AND content IS NOT NULL AND size_bytes IS NOT NULL AND storage_uri IS NULL AND length(content)=size_bytes)
           OR (storage_kind IN ('file','object') AND content IS NULL AND storage_uri IS NOT NULL)),
        CHECK(state!='available' OR (verified_at IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL)));
CREATE TABLE migration_findings (
        property_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, id TEXT NOT NULL,
        json_pointer TEXT NOT NULL, code TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
        details_json TEXT NOT NULL CHECK(json_valid(details_json)), PRIMARY KEY(property_id,id),
        FOREIGN KEY(property_id,snapshot_id) REFERENCES source_snapshots(property_id,id));
CREATE TABLE notification_deliveries (
        property_id TEXT NOT NULL, id TEXT NOT NULL, event_id TEXT NOT NULL,
        channel TEXT NOT NULL, recipient_ref TEXT NOT NULL, status TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt>=0), updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(property_id,id),
        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));
CREATE TABLE pass_requests (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, vehicle_id TEXT NOT NULL REFERENCES vehicles(id), stay_id TEXT NOT NULL REFERENCES stays(id), status TEXT NOT NULL CHECK(status IN ('requested','approved','returned','denied','queued','processing','active','failed','expired','deleted')), version INTEGER NOT NULL CHECK(version>0), provider_operation_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE passes (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, vehicle_id TEXT NOT NULL REFERENCES vehicles(id), stay_id TEXT NOT NULL REFERENCES stays(id), request_id TEXT NOT NULL UNIQUE REFERENCES pass_requests(id), status TEXT NOT NULL CHECK(status IN ('active','expired','deleted','failed')), valid_from TEXT NOT NULL, valid_until TEXT NOT NULL, provider_ref TEXT, version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE payments (id TEXT PRIMARY KEY, quote_id TEXT NOT NULL REFERENCES billing_quotes(id), kind TEXT NOT NULL CHECK(kind IN ('advance','payment','refund','deposit','deposit_refund')), amount_minor INTEGER NOT NULL CHECK(amount_minor>0), actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE properties (id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), timezone TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE provider_operations (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, provider TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('create_pass','delete_pass','list_passes','status')), aggregate_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','processing','succeeded','failed','dead_letter')), attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0), available_at TEXT NOT NULL, lease_until TEXT, request_json TEXT NOT NULL, response_json TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE rate_plans (property_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(property_id,id,version));
CREATE TABLE recovery_operations (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, incident_id TEXT NOT NULL REFERENCES incidents(incident_id), action TEXT NOT NULL CHECK(action IN ('fallback','reload','restart','notify')), safety_class TEXT NOT NULL CHECK(safety_class IN ('informational','physical')), status TEXT NOT NULL CHECK(status IN ('queued','processing','succeeded','dead_letter','cancelled')), attempt INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_until TEXT, request_json TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE relax47_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "relax47_meta" VALUES('schema_version','10');
CREATE TABLE runtime_documents (
        property_id TEXT NOT NULL REFERENCES properties(id), namespace TEXT NOT NULL, item_key TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version>0), updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,namespace,item_key));
CREATE TABLE sensor_observations (
        event_id TEXT PRIMARY KEY REFERENCES event_context(event_id), entity_id TEXT NOT NULL,
        old_state_json TEXT CHECK(old_state_json IS NULL OR json_valid(old_state_json)),
        new_state_json TEXT NOT NULL CHECK(json_valid(new_state_json)),
        attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)));
CREATE TABLE source_mappings (
        property_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, json_pointer TEXT NOT NULL,
        target_table TEXT NOT NULL, target_key_json TEXT NOT NULL CHECK(json_valid(target_key_json)),
        status TEXT NOT NULL CHECK(status IN ('mapped','preserved','unresolved')),
        PRIMARY KEY(property_id,snapshot_id,json_pointer,target_table),
        FOREIGN KEY(property_id,snapshot_id) REFERENCES source_snapshots(property_id,id));
CREATE TABLE source_snapshots (
        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,
        source_key TEXT NOT NULL, captured_at TEXT NOT NULL, imported_at TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK(length(sha256)=64), size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
        content BLOB NOT NULL, PRIMARY KEY(property_id,id), UNIQUE(property_id,source_key,sha256),
        CHECK(length(content)=size_bytes));
CREATE TABLE stay_commands (actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(actor,request_id));
CREATE TABLE stay_guests (
        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, guest_id TEXT NOT NULL,
        role TEXT NOT NULL, position INTEGER NOT NULL CHECK(position>=0),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,stay_id,guest_id),
        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),
        FOREIGN KEY(property_id,guest_id) REFERENCES guests(property_id,id));
CREATE TABLE stay_reviews (
        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, id TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,stay_id,id),
        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id));
CREATE TABLE stay_spa_sessions (
        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position>=0), start_at TEXT, end_at TEXT,
        timezone TEXT, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,stay_id,id),
        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id));
CREATE TABLE stay_vehicles (
        property_id TEXT NOT NULL, stay_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position>=0), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,stay_id,vehicle_id),
        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),
        FOREIGN KEY(property_id,vehicle_id) REFERENCES vehicles(property_id,id));
CREATE TABLE stay_violations (
        property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL,
        stay_id TEXT, zone_id TEXT, rule_id TEXT, status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(property_id,id),
        FOREIGN KEY(property_id,stay_id) REFERENCES stays(property_id,id),
        FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id));
CREATE TABLE stays (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), check_in TEXT NOT NULL, check_out TEXT NOT NULL, reserved_end TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE vehicles (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, plate TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(property_id,plate));
CREATE TABLE violation_events (
        property_id TEXT NOT NULL, violation_id TEXT NOT NULL, event_id TEXT NOT NULL,
        PRIMARY KEY(property_id,violation_id,event_id),
        FOREIGN KEY(property_id,violation_id) REFERENCES stay_violations(property_id,id),
        FOREIGN KEY(property_id,event_id) REFERENCES event_context(property_id,event_id));
CREATE TABLE violation_media (
        property_id TEXT NOT NULL, violation_id TEXT NOT NULL, media_id TEXT NOT NULL, role TEXT NOT NULL,
        PRIMARY KEY(property_id,violation_id,media_id,role),
        FOREIGN KEY(property_id,violation_id) REFERENCES stay_violations(property_id,id),
        FOREIGN KEY(property_id,media_id) REFERENCES media_assets(property_id,id));
CREATE TABLE zone_configuration (
        property_id TEXT NOT NULL, zone_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        position INTEGER NOT NULL CHECK(position>=0), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        PRIMARY KEY(property_id,zone_id,revision),
        FOREIGN KEY(property_id,zone_id) REFERENCES zones(property_id,id));
CREATE TABLE zones (property_id TEXT NOT NULL REFERENCES properties(id), id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(property_id,id));
CREATE INDEX idx_events_type_time
            ON events(event_type, occurred_at);
CREATE INDEX idx_events_correlation
            ON events(correlation_id);
CREATE INDEX idx_stays_property_period ON stays(property_id,check_in,reserved_end);
CREATE INDEX idx_quotes_stay ON billing_quotes(property_id,stay_id);
CREATE INDEX idx_payments_quote ON payments(quote_id);
CREATE INDEX idx_vehicles_property ON vehicles(property_id,id);
CREATE INDEX idx_pass_requests_stay ON pass_requests(property_id,stay_id,status);
CREATE INDEX idx_passes_vehicle_period ON passes(property_id,vehicle_id,valid_from,valid_until);
CREATE INDEX idx_provider_operations_due ON provider_operations(provider,status,available_at);
CREATE INDEX idx_gate_events_vehicle_time ON gate_events(property_id,vehicle_id,occurred_at);
CREATE INDEX idx_access_decisions_stay_time ON access_decisions(property_id,stay_id,evaluated_for);
CREATE INDEX idx_incidents_property_status ON incidents(property_id,status,severity);
CREATE INDEX idx_recovery_due ON recovery_operations(status,available_at);
CREATE UNIQUE INDEX ux_stays_property_id ON stays(property_id,id);
CREATE UNIQUE INDEX ux_vehicles_property_id ON vehicles(property_id,id);
CREATE UNIQUE INDEX ux_passes_property_id ON passes(property_id,id);
CREATE UNIQUE INDEX ux_gate_events_property_id ON gate_events(property_id,id);
CREATE INDEX idx_event_context_stay ON event_context(property_id,stay_id,event_id);
CREATE INDEX idx_event_context_problem ON event_context(property_id,severity,category,event_id);
CREATE INDEX idx_events_time ON events(occurred_at,event_id);
CREATE INDEX idx_event_context_vehicle ON event_context(property_id,vehicle_id,event_id);
CREATE INDEX idx_event_context_zone ON event_context(property_id,zone_id,event_id);
CREATE INDEX idx_sensor_observations_entity ON sensor_observations(entity_id,event_id);
CREATE INDEX idx_violations_stay ON stay_violations(property_id,stay_id,status);
CREATE VIEW system_journal AS
        SELECT e.event_id,e.event_type,e.occurred_at,e.correlation_id,e.actor,e.payload_json,
               c.property_id,c.category,c.severity,c.stay_id,c.vehicle_id,c.pass_id,c.zone_id,
               c.sensor_entity_id,c.parent_event_id,c.received_at
        FROM events e LEFT JOIN event_context c ON c.event_id=e.event_id;
COMMIT;
