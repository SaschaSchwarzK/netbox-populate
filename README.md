# NetBox extra demo data

Populates a NetBox instance with additional, **fictional** data on top of the official
[netbox-demo-data](https://github.com/netbox-community/netbox-demo-data). It adds the object types the demo data
does not contain at all (virtual chassis, VDCs, MAC addresses, tunnels, L2VPNs, wireless, virtual circuits,
FHRP, rack types, inventory items, config contexts, event rules, ...) and exercises their relations, enumerations and
custom-field data types.

The engine (`populate.py`) is generic. All data lives in small YAML files in `data/`, one file per API endpoint, so it
is easy to extend.

Tested against **NetBox v4.6.10** with `netbox-demo-v4.6.sql` and **pynetbox 7.9.0**: a full run on the pristine demo
database creates 419 objects with no failures, a second run creates nothing, and `--cleanup` removes everything again.
Names come from Star Wars, Star Trek, Harry Potter, Lord of the Rings, Game of Thrones, Batman and other series; all
addresses, keys and passwords are made up.

## Quick start

```bash
pip install -r requirements.txt
export NETBOX_URL=https://netbox.example.com
export NETBOX_TOKEN=...            # v1 token or v2 "nbt_<key>.<secret>" token, needs write access

python populate.py --validate      # offline check of the data files, no NetBox needed
python populate.py --dry-run       # show what would be created
python populate.py                 # create the data (safe to run again)
python populate.py -v              # also print every object
python populate.py --cleanup       # delete what the files describe (test instances only!)
```

Prefer the environment variable for the token: a token on the command line ends up in the shell history.

| Option | Meaning |
|---|---|
| `--only REGEX` / `--skip REGEX` | process only / skip files whose name matches (e.g. `--only 'vpn\|wireless'`) |
| `--list` | list the selected files |
| `--dry-run` | no writes; objects that depend on objects not yet created are counted at the end |
| `--cleanup` | delete the objects in reverse order and reset fields set by `update` files |
| `--fail-fast` | stop at the first failure (default: report all failures, exit code 1) |
| `--insecure`, `--ca-bundle FILE` | TLS options |
| `--validate` | structure and reference syntax check, no network |

Use a **test instance**. The script creates users, permissions, webhooks, custom fields on devices/sites/VMs and
other objects that you do not want in production.

## What it adds

| Area | Objects |
|---|---|
| Extras | custom field choice sets (incl. base choices IATA / UN-LOCODE / ISO 3166 and colours), custom fields of **every type** (text, long text, integer, decimal, boolean, date, datetime, URL, JSON with schema, select, multi-select, object, multi-object), tags, config context profiles, config contexts (every assignment relation), config templates, export templates, custom links, saved filters, table configs, webhooks, event rules, notification groups, journal entries, bookmarks, subscriptions, notifications |
| Core / Users | data sources, groups, users, owner groups, owners (ownership), object permissions with constraints |
| Tenancy | tenant groups, tenants, contact groups, roles, contacts, contact assignments (generic relation) |
| DCIM | regions, site groups, sites (every status, time zones, coordinates, ASNs, custom fields), locations, manufacturers, rack groups, rack types, racks, device roles and platforms (nested), device types (parent/child, airflow, weights, fractional U), **all component templates** (interface types incl. PoE/bridge/RF, console server, power, rear/front port mappings, device/module bays, inventory item templates), devices in every status (virtual chassis, device bays, VDCs, local context data, custom fields), extra interfaces (LAG, sub-interfaces, WWN, RF channel, Q-in-Q, VLAN modes, VRF, primary MAC), MAC addresses, inventory items (nested, linked to components), console server ports, cable bundles, cables (every relevant type, several terminations per side, circuit terminations) |
| IPAM | RIRs, ASN ranges, ASNs, route targets, VRFs, roles, VLAN groups (every scope type), VLANs (every status, Q-in-Q), aggregates, prefixes (every status and scope), IP ranges, FHRP groups and assignments, IP addresses (every status and role, NAT, FHRP/VM/device assignment), services and service templates, VLAN translation policies and rules |
| Virtualization | cluster types/groups, clusters (every status and scope), VM types, virtual machines (every status and start-on-boot mode, host device), VM interfaces, virtual disks |
| Wireless | wireless LAN groups, SSIDs (every status, auth type and cipher), wireless links |
| VPN | IKE/IPsec proposals, policies and profiles, tunnel groups, tunnels (every encapsulation), tunnel terminations, L2VPNs (every type) and terminations |
| Circuits | providers, provider accounts, provider networks, circuit types, circuits (every status), circuit groups and assignments, circuit terminations (site / location / region / site group / provider network), virtual circuit types, virtual circuits and terminations |

Not covered because they cannot be created through the REST API in a useful way: scripts and managed files (uploaded
files), data files and auto-sync records (created by a data source sync), config revisions, dashboards (per-user layout)
and image attachments (multipart upload).

Side effects: the webhook-based event rule and the git data source are created **disabled** so that no requests go to
the fake hosts. Users get fake passwords (see `data/050-users.users.yml`); change them or do not load that file on a
shared instance (`--skip '050-users'`).

## How it works

Files in `data/` are processed in **filename order**; the numeric prefix defines the dependency order. Each file
describes one endpoint:

```yaml
endpoint: dcim/sites            # "<app>/<endpoint>" as in the REST API path
lookup: [slug]                  # fields that identify an existing object (default: [name])
mode: create                    # create (default) | update | upsert
defaults: {status: active}      # merged into every object of the file
plain_tags: false               # true where "tags" is a plain list of slugs (config contexts)
lookup_mode: filter             # "scan": list everything and compare locally (endpoints without filters)
objects:
  - name: Hogwarts Castle
    slug: hogwarts-castle
    region: "@dcim/regions?slug=middle-earth"
    tags: [pied-piper]          # tag slugs; the engine turns them into references
```

* **create** creates the object unless one matching `lookup` exists. Running the script again is therefore safe.
* **update** patches an existing object. Used for relations that need both objects first (`primary_ip4`, a virtual
  chassis `master`, a device bay's `installed_device`).
* **upsert** creates or patches.

### References

Any string starting with `@` is replaced by the id of the object it points to:

```yaml
site: "@dcim/sites?slug=death-star-ii"
rack: "@dcim/racks?name=DS2-R01&site=death-star-ii"       # several filters: &
tagged_vlans: ["@ipam/vlans?name=A", "@ipam/vlans?name=B"]  # works in lists
assigned_object_type: dcim.interface                        # generic relation: type ...
assigned_object_id: "@dcim/interfaces?device=nexus-core-01&name=mgmt0"   # ... plus id
```

The filter must match exactly one object. Use `%26` for a literal `&` and `@@` for a literal leading `@`.
Objects without a unique name (component templates) use the mapping form, which can nest references:

```yaml
power_port: {$ref: dcim/power-port-templates, device_type_id: "@dcim/device-types?slug=acme-rocket-pdu-12", name: Inlet}
```

Endpoints without a filter set (`extras/notification-groups`, `extras/subscriptions`, `extras/notifications`) need
`lookup_mode: scan` or `{$ref: ..., $scan: true, ...}`.

### Safety nets

NetBox silently ignores unknown filter names, which would return an unrelated object. After every lookup the engine
compares the found object with the lookup values and fails loudly if they differ, so a typo in a filter name cannot
update or skip the wrong object. Failures are reported per object (`FAIL ...`) and the exit code is 1.

## Adding data

1. Add objects to an existing file, or create `NNN-<app>.<endpoint>.yml` with a prefix that sorts after the files it
   depends on (leave gaps in the numbering).
2. Look up writable fields with the interactive API docs at `/api/docs/` of your instance.
3. `python populate.py --validate`, then `python populate.py --only '^NNN' -v`.
4. `pytest tests/` runs the offline engine tests (they also validate all data files).

YAML pitfalls: quote `"on"`/`"off"`/`"yes"`/`"no"` values (bare, they are booleans), quote strings ending in `?` inside
`{...}`, and quote MAC addresses and route targets that contain colons.

## Known quirks (NetBox 4.6.10)

* **Nested inventory item templates break device creation.** When a device is created from a device type whose inventory
  item templates have a `parent`, NetBox instantiates children before parents and the API answers `403`. The data
  therefore uses flat inventory item templates; nested inventory *items* (not templates) work and are included.
* Subscriptions and notifications are only visible to their own user, so they are created for the user `admin`.
  Edit `data/993-*.yml` and `data/994-*.yml` if your token belongs to another user.
* Custom fields are attached to `dcim.site`, `dcim.device` and `virtualization.virtualmachine`, so they appear on every
  such object of the instance.
* A re-run reports a few `updated` objects: `update`/`upsert` files send their PATCH again.
* Other NetBox versions: endpoints that do not exist are skipped, but a field that differs between versions
  fails for that object only.
