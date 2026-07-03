"""Generate synthetic compliance fixtures for Phase 1 of the KYC gap-analysis
feature: a fictional NBFC's internal KYC policy + a condensed RBI KYC requirements
doc. Both are PDFs (real page numbers → citable). The policy is deliberately
seeded so the gap analysis shows all four states:

  Covered  - OVD identification, risk categorisation, CDD, Principal Officer
  Partial  - V-CIP (missing some conditions), EDD (generic)
  Gap      - periodic updation has no 2-year high-risk cycle; no beneficial
             ownership; no FIU-IND reporting
  Conflict - record retention 3 years (RBI requires 5)

Run:  python -m scripts.make_sample_compliance_docs
Real RBI Master Direction on KYC swaps in for the regulation doc at Phase 2.
"""

from pathlib import Path

import fitz  # PyMuPDF

_OUT = Path(__file__).parent.parent / "data" / "compliance"

# Each "page" is a (title, body) pair. Body wraps inside a text box.
_RBI_PAGES = [
    ("RBI KYC - Key Requirements (condensed for demo)",
     "This document condenses key obligations from the RBI Master Direction on "
     "Know Your Customer (KYC). It is a synthetic study aid for building the "
     "compliance tool, not the official text.\n\n"
     "Section 1. Customer identification. Every customer must be identified "
     "using an Officially Valid Document (OVD) such as passport, Aadhaar, voter "
     "ID, driving licence or NREGA job card at the time of commencement of an "
     "account-based relationship.\n\n"
     "Section 2. Risk categorisation. Regulated entities shall categorise every "
     "customer as low, medium or high risk based on a risk assessment, and apply "
     "due diligence proportionate to the assessed risk.\n\n"
     "Section 3. Customer Due Diligence (CDD). CDD shall be carried out at the "
     "commencement of the relationship, when there are doubts about previously "
     "obtained data, and when a specified transaction threshold is crossed."),
    ("RBI KYC - Key Requirements (continued)",
     "Section 4. Enhanced Due Diligence (EDD). For customers assessed as high "
     "risk, enhanced due diligence measures shall be applied, including obtaining "
     "the source of funds and closer ongoing monitoring.\n\n"
     "Section 5. Periodic updation. KYC records shall be periodically updated: at "
     "least once every two years for high-risk customers, once every eight years "
     "for medium-risk, and once every ten years for low-risk customers.\n\n"
     "Section 6. Video-based Customer Identification Process (V-CIP). V-CIP is "
     "permitted for remote onboarding provided the process is live, the official "
     "records the customer's live location (geo-tagging), verifies the OVD, and "
     "the entire interaction is recorded and stored.\n\n"
     "Section 7. Beneficial ownership. For legal-entity customers, the beneficial "
     "owner(s) shall be identified and their identity verified."),
    ("RBI KYC - Key Requirements (continued)",
     "Section 8. Record retention. Records of the identity of clients and of "
     "transactions shall be maintained for at least five years after the "
     "business relationship ends or the account is closed.\n\n"
     "Section 9. Principal Officer. Every regulated entity shall appoint a "
     "Principal Officer responsible for compliance, monitoring and reporting.\n\n"
     "Section 10. Reporting to FIU-IND. Suspicious Transaction Reports (STRs) and "
     "Cash Transaction Reports (CTRs) shall be filed with the Financial "
     "Intelligence Unit-India (FIU-IND) within the prescribed timelines."),
]

_POLICY_PAGES = [
    ("Acme Finance NBFC - Internal KYC Policy",
     "Acme Finance Private Limited is committed to preventing money laundering "
     "and to complying with applicable KYC norms. This policy sets out how we "
     "identify and monitor our customers.\n\n"
     "1. Customer identification. At onboarding, every customer must submit an "
     "Officially Valid Document (OVD) - passport, Aadhaar, voter ID or driving "
     "licence. No account is opened without a verified OVD on file.\n\n"
     "2. Risk categorisation. Each customer is classified as low, medium or high "
     "risk at onboarding based on occupation, geography and expected transaction "
     "profile. Due diligence is applied in proportion to the risk category.\n\n"
     "3. Customer Due Diligence. CDD is performed for every customer at the "
     "start of the relationship and whenever we doubt the accuracy of previously "
     "collected information."),
    ("Acme Finance NBFC - Internal KYC Policy (continued)",
     "4. Enhanced measures. For customers who appear to carry higher risk, "
     "additional checks may be carried out at the discretion of the compliance "
     "team.\n\n"
     "5. Updation of records. Customer KYC information is reviewed and updated "
     "from time to time based on the customer's risk category, so that our "
     "records remain current.\n\n"
     "6. Remote onboarding. Customers may be onboarded remotely through a video "
     "call in which an officer verifies the customer's OVD and photograph.\n\n"
     "7. Record retention. Records of customer identity and transactions are "
     "retained for a period of three years after the account is closed."),
    ("Acme Finance NBFC - Internal KYC Policy (continued)",
     "8. Governance. The company has appointed a Principal Officer who is "
     "responsible for overseeing KYC compliance and for internal monitoring of "
     "customer accounts.\n\n"
     "9. Training. Staff involved in onboarding receive periodic training on the "
     "company's KYC procedures.\n\n"
     "10. Review. This policy is reviewed by senior management at least once a "
     "year and updated as required."),
]


def _write_pdf(path: Path, pages) -> None:
    doc = fitz.open()
    for title, body in pages:
        page = doc.new_page()  # A4 default
        rect = fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
        text = f"{title}\n\n{body}"
        page.insert_textbox(rect, text, fontsize=11, fontname="helv", lineheight=1.4)
    doc.save(str(path))
    doc.close()


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    reg = _OUT / "rbi_kyc_requirements.pdf"
    pol = _OUT / "acme_kyc_policy.pdf"
    _write_pdf(reg, _RBI_PAGES)
    _write_pdf(pol, _POLICY_PAGES)
    print(f"wrote {reg} ({len(_RBI_PAGES)} pages)")
    print(f"wrote {pol} ({len(_POLICY_PAGES)} pages)")


if __name__ == "__main__":
    main()
