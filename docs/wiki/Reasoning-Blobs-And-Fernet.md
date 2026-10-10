# Encrypted reasoning

Some saved reasoning items contain `encrypted_content`. A different provider may reject those items. Removing them can make a session usable elsewhere, but it loses information and does not guarantee equivalent behavior.

The cryptographic format is not established by the field's appearance. Keep the original session and follow [recovery](Recovery.md).

The manager's Messages adapter preserves exact signed or redacted blocks in authenticated encrypted continuations. New continuations also carry an encrypted check of the translated instructions, tools and complete retained history before each block. A replay with an edited prefix is refused before upstream HTTP; it is not repaired by removing reasoning. Changing future output limits or summary display does not edit that prefix. Older continuations keep their original connection/model binding without this additional check.

Keep the continuation key and checksum in the original Codex home private and available for later replay. Offline continuation authentication checks the selected connection and model; the actual replay separately checks its prefix. These local checks cannot attest a gateway's backend account, effective model or fetched image bytes. See [compatibility](Compatibility.md) for current protocol limits and [privacy](Security-And-Privacy-Guide.md) before sharing sessions.
