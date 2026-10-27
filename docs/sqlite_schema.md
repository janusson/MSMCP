# SQLite Peak Store Schema

MSMCP supports reading spectral libraries from a local SQLite peak store (files ending in `.sqlite` or `.db`). This allows for fast, indexed, and chunked access to large spectral datasets without parsing text files.

## Schema Definition

The SQLite database must be checkpointed and sidecar-free (no `-wal`, `-shm`, or `-journal` files). It must contain the following two tables:

### `spectra` Table

| Column | Type | Description |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | The unique identifier for the spectrum. |
| `compound_name` | `TEXT` | The name of the compound or spectrum title. |
| `precursor_mz` | `REAL` | The precursor m/z value. |
| `formula` | `TEXT` | The molecular formula (if known). |
| `polarity` | `INTEGER` | The ionization polarity: `+1` for positive, `-1` for negative, and `NULL` for unknown. |
| `metadata` | `JSON` | A JSON object containing string key-value pairs for additional metadata (e.g., `"MSLEVEL": "2"`). |

### `peaks` Table

| Column | Type | Description |
|---|---|---|
| `spectrum_id` | `INTEGER` | A foreign key referencing `spectra(id)`. |
| `mz` | `REAL` | The m/z value of the peak. |
| `intensity` | `REAL` | The intensity of the peak. |

*(Must include `FOREIGN KEY(spectrum_id) REFERENCES spectra(id)`)*


## Building a SQLite Peak Store

You can build a SQLite peak store from an existing text-based library (like MSP or MGF) using the Python snippet below. This script uses MSMCP's built-in text readers to parse the file and insert the data into a SQLite database.

```python
import json
import sqlite3
from pathlib import Path
from msmcp.security import SecurityPolicy
from msmcp.library import get_library_provider

def build_sqlite_store(input_text_library: Path, output_sqlite_db: Path) -> None:
    # 1. Create the database and schema
    conn = sqlite3.connect(output_sqlite_db)
    conn.execute(
        """
        CREATE TABLE spectra (
            id INTEGER PRIMARY KEY,
            compound_name TEXT,
            precursor_mz REAL,
            formula TEXT,
            polarity INTEGER,
            metadata JSON
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE peaks (
            spectrum_id INTEGER,
            mz REAL,
            intensity REAL,
            FOREIGN KEY(spectrum_id) REFERENCES spectra(id)
        )
        """
    )
    # Recommended for performance
    conn.execute("CREATE INDEX idx_peaks_spectrum_id ON peaks(spectrum_id)")

    # 2. Get the provider for the text library (MSP or MGF)
    # Using a loose security policy for the build script since it's a user action
    policy = SecurityPolicy(allowed_root=input_text_library.parent)
    provider = get_library_provider(str(input_text_library), policy)

    # 3. Read spectra in chunks and insert them
    cursor = conn.cursor()
    conn.execute("BEGIN TRANSACTION")
    try:
        for chunk in provider.iter_spectra(chunk_size=1000):
            for spectrum in chunk:
                # Map string polarity back to integers
                polarity_int = None
                if spectrum.polarity == "positive":
                    polarity_int = 1
                elif spectrum.polarity == "negative":
                    polarity_int = -1

                metadata_json = json.dumps(dict(spectrum.metadata))

                cursor.execute(
                    """
                    INSERT INTO spectra (id, compound_name, precursor_mz, formula, polarity, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spectrum.index,
                        spectrum.compound_name,
                        spectrum.precursor_mz,
                        spectrum.formula,
                        polarity_int,
                        metadata_json
                    )
                )

                # Insert peaks
                peak_rows = [
                    (spectrum.index, mz, intensity)
                    for mz, intensity in spectrum.peaks
                ]
                cursor.executemany(
                    "INSERT INTO peaks (spectrum_id, mz, intensity) VALUES (?, ?, ?)",
                    peak_rows
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    # Example usage:
    # build_sqlite_store(Path("my_library.msp"), Path("my_library.sqlite"))
    pass
```
