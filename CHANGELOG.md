# Changelog

## 1.1.2

- Recover from network failures during alarm-server setup using Home Assistant's normal setup retry. Authentication failures in this stage request reauthentication.
- Register NVR camera links using the parent's Home Assistant device ID. Standalone cameras no longer send a deprecated empty parent field.
- Use valid default IDs for diagnostic/storage sensors and the correct image domain for snapshots. Entity unique IDs remain unchanged.
- Route notifications only through loaded entries. Failed entries no longer block healthy cameras, and notifications without a configured sensor no longer flood the warning log.
- Register the HTTP notification listener once during integration setup, independent of which camera loads first.
- Keep diagnostic sensors readable when an alarm-host poll returns no data.

Requires Home Assistant 2026.8 or later. Tests target Home Assistant 2026.9.1 on Python 3.14.
