#!/bin/bash
# PROTOTYPE for issue 9: render every variant to PDF with WeasyPrint (BSD), twice, and check the
# two renders are byte-identical. Run in a throwaway container from the repository root:
#   docker run --rm -v "$PWD:/work" python:3.12-slim-bookworm bash /work/prototype/report/pdf.sh
set -e
apt-get update -qq && apt-get install -y -qq --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core >/dev/null
pip install -q weasyprint==63.1
cd /work/prototype/report/out
export SOURCE_DATE_EPOCH=1792237320   # the run's finish time; the PDF carries no clock of its own
for pass in 1 2; do
  for f in signoff-A signoff-B signoff-C engineering-A engineering-B; do weasyprint -q $f.html $f.pdf; done
  sha256sum *.pdf > /tmp/pass$pass.txt
done
diff -q /tmp/pass1.txt /tmp/pass2.txt && echo "PDFs byte-identical across two renders"
cat /tmp/pass2.txt
