# Third-party notices

Inferdrome's packaged dashboard contains runtime assets built from the exact
production dependency graph locked in `frontend/package-lock.json`. These
components retain their own licenses; Inferdrome's Apache-2.0 license does not
replace or expand them.

| Component | Locked version | License | Packaged license text |
| --- | ---: | --- | --- |
| Instrument Sans (`@fontsource-variable/instrument-sans`) | 5.3.0 | SIL Open Font License 1.1 | `LICENSES/OFL-1.1-Instrument-Sans.txt` |
| IBM Plex Mono (`@fontsource/ibm-plex-mono`) | 5.3.0 | SIL Open Font License 1.1 | `LICENSES/OFL-1.1-IBM-Plex-Mono.txt` |
| Lucide React (`lucide-react`) | 0.468.0 | ISC | `LICENSES/ISC-Lucide.txt` |
| React (`react`) | 19.2.8 | MIT | `LICENSES/MIT-React.txt` |
| React DOM (`react-dom`) | 19.2.8 | MIT | `LICENSES/MIT-React.txt` |
| Scheduler (`scheduler`) | 0.27.0 | MIT | `LICENSES/MIT-React.txt` |
| Vite module-preload runtime (`vite`) | 7.3.6 | MIT | `LICENSES/MIT-Vite.txt` |

The copyright notices and license texts were retained from the corresponding
package archives identified by the committed lockfile. The production bundle
also retains Vite's injected module-preload runtime and its upstream license
file. Other build- and test-only frontend dependencies are outside this
runtime notice inventory.

This notice does not make or resolve licensing decisions for models,
tokenizers, benchmark workloads, serving engines, generated output, raw
evidence archives, container base images, or other externally supplied
materials.
