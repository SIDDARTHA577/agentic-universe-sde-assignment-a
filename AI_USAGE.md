# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Google Antigravity**
- **ChatGPT**

Google Antigravity was used as the AI-assisted coding and testing environment.

ChatGPT was used to help understand the assignment requirements, choose Assignment A, prepare implementation instructions, and outline verification requirements.

## What I used them for
I used AI tools to:
- Understand and organize the assignment requirements and specifications.
- Plan the architectural and implementation steps.
- Develop and refine the Python modules under `solution/`.
- Implement and structure the database, lead ingestion, append-only event ledger, and reporting components.
- Generate and expand the automated test suite in `tests/`.
- Investigate and correct implementation issues discovered during testing.
- Review and refine documentation.
- Check the implementation against the assignment requirements and output contracts.

## What I wrote or decided myself
I selected Assignment A and defined the requirements and expected behaviour for the implementation. I guided the development process using Antigravity and requested additional testing to check correctness, security, database integrity and edge cases. I reviewed the testing reports and directed further corrections and verification.

## Where the AI was wrong or unhelpful
During testing, two issues were identified. Nested personal information in event payloads was not being fully sanitized, and optional phone fields could be rejected incorrectly. I used Antigravity to correct these issues and requested additional tests. After the fixes, the final test suite reported 77 passing tests with no failures.

## How I verified AI-generated code
I used Antigravity to run the automated tests and review the implementation against the assignment requirements. After the initial 64 tests passed, I requested a more detailed audit covering additional edge cases, data privacy, database integrity and different test datasets. The final verification reported 77 passing tests across the implemented test suite. I also reviewed the audit results and documented the remaining limitations.
