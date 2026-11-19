# Independent mzML inputs

The three mzML files come from the OpenMS repository at commit
`b4234c49110f80aee7b0adfb2a9ae6e01f441605`. The upstream BSD three-clause
notice is included in OpenMS-LICENSE.txt. Pytest pins the SHA-256 of each file
before checking its source declarations. The yeast and sparse files are byte-identical
to upstream. The distributed Let-7 file removes the indexed wrapper and redacts
the source-file location and the processing method's input/output paths. Its
spectrum and instrument configuration are byte-identical to the upstream
elements. The upstream SHA-256 is
`20a932b27961deb82b35f7986437785c5922d4a11afb6b3e0a1b95d0f8e7c065`;
the distributed file's SHA-256 is
`77da1ed84210d5ef6fdec15304f1bb6ffbb4259117fba5e8c1e2a17326e8080a`.

| Local name | Upstream file | Evidence |
| --- | --- | --- |
| let7.mzML | [NucleicAcidSearchEngine_1.mzML](https://github.com/OpenMS/OpenMS/blob/b4234c49110f80aee7b0adfb2a9ae6e01f441605/doc/pyopenms/src/data/NucleicAcidSearchEngine_1.mzML) | Filtered spectrum from minimal_01a.raw, scan 88. Index 0 declares beam-type CID and collision energy 22 eV, negative polarity, nanoelectrospray and two analyser components. |
| yeast.mzML | [SimpleSearchEngine_1.mzML](https://github.com/OpenMS/OpenMS/blob/b4234c49110f80aee7b0adfb2a9ae6e01f441605/doc/pyopenms/src/data/SimpleSearchEngine_1.mzML) | Three processed yeast spectra from KKramer_150612_yeastTAP_4_120min.raw. Index 0 declares generic activation energy 0 eV and free-text collision energy 45 without a unit. The collision-energy CV term is absent. |
| sparse-conversion.mzML | [FileConverter_2_output.mzML](https://github.com/OpenMS/OpenMS/blob/b4234c49110f80aee7b0adfb2a9ae6e01f441605/src/tests/topp/FileConverter_2_output.mzML) | External DTA2D conversion fixture. Its configuration contains no instrument components. Original experimental lineage is unverified. |

To inspect exactly what the two acquisition files declare, run this independent
libxml2 XPath from this directory. It requires no MSMCP installation.

```sh
for file in let7.mzML yeast.mzML; do
    xmllint --xpath "//*[local-name()='spectrum' and @index='0']/*[local-name()='precursorList']/*[local-name()='precursor']/*[local-name()='activation']" "$file"
done
```

The automated oracle checks the distributed digest, scoped XML and expected
accessions, values and units independently of MSMCP's reader. The multi-scan,
conflict and malformed-value cases in test_acquisition.py edit a temporary
copy. They test handling of specific inputs and provide no independent
measurement of instrument behavior.
