"""Shared file and transport limits; no geometry imports needed by middleware."""

MIB = 1024 * 1024
MAX_FILE_BYTES = 50 * MIB
MAX_EXACT_FILE_BYTES = 256 * MIB
UPLOAD_MAX_BYTES = MAX_EXACT_FILE_BYTES + 8 * MIB  # multipart fields and framing
