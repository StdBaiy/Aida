Patch protocol:
- Before updating or deleting a file, read it and pass its sha256 to apply_patch.
- Keep each apply_patch focused on one file and below 12,000 argument characters.
  Calls above 32,000 characters or adding multiple new files are rejected.
- Split changes before approaching the model output limit. Create large files as a small valid
  skeleton, then read their current hash and extend them with sequential updates.
- Build long test files in logical sections and verify the completed change.
