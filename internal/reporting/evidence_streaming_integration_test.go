//go:build integration

package reporting

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"

	"cheesetrap/internal/domain"
	"cheesetrap/internal/store/db"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type streamedCopyCapture struct {
	pgx.Tx
	copies, seals, copiesAtSeal int
}

func (c *streamedCopyCapture) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if strings.Contains(sql, "-- name: CopyReportEvidenceBatch :exec") {
		c.copies++
	}
	return c.Tx.Exec(ctx, sql, args...)
}

func (c *streamedCopyCapture) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if strings.Contains(sql, "SELECT cheesetrap.seal_report_proof($1)") {
		c.seals++
		if err := c.Tx.QueryRow(ctx, `SELECT count(*) FROM cheesetrap.report_evidence WHERE report_id=$1`, args[0]).Scan(&c.copiesAtSeal); err != nil {
			c.copiesAtSeal = -1
		}
	}
	return c.Tx.QueryRow(ctx, sql, args...)
}

// Reuse the existing real shared-chunk fixture, including all 1024 members and
// 256 rules. Each savepoint rolls back so subsequent ownership checks see it intact.
func verifyStreamedEvidenceCopies(t *testing.T, ctx context.Context, tx pgx.Tx, window db.CheesetrapReportWindow) {
	t.Helper()
	expected, err := collectEvidence(ctx, db.New(tx), window, "1.1.1.1")
	if err != nil || len(expected.Evidence) != 1024 {
		t.Fatal("incomplete source fixture", err)
	}
	for _, fail := range []bool{false, true} {
		name := "bounded_copies_preserve_signed_parts"
		if fail {
			name = "later_copy_failure_rolls_back_before_sealing"
		}
		t.Run(name, func(t *testing.T) {
			lane, err := tx.Begin(ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer lane.Rollback(context.Background())
			var report string
			if err := lane.QueryRow(ctx, `INSERT INTO cheesetrap.reports(window_id,source_ip) VALUES($1,'1.1.1.1') RETURNING report_id::text`, window.WindowID).Scan(&report); err != nil {
				t.Fatal(err)
			}
			if fail {
				_, err = lane.Exec(ctx, `CREATE FUNCTION cheesetrap.test_streamed_copy_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
IF NEW.snapshot->>'ingestion_seq'='1025001' THEN RAISE EXCEPTION 'streamed_copy_fault'; END IF; RETURN NEW; END $$;
CREATE TRIGGER test_streamed_copy_failure BEFORE INSERT ON cheesetrap.report_evidence FOR EACH ROW EXECUTE FUNCTION cheesetrap.test_streamed_copy_failure()`)
				if err != nil {
					t.Fatal(err)
				}
			}
			capture := &streamedCopyCapture{Tx: lane}
			snapshot, err := collectProof(ctx, db.New(capture), window, "1.1.1.1", report)
			if fail {
				if err == nil || capture.copies != 2 || capture.seals != 0 {
					t.Fatal("later copy failure must stop before seal", capture.copies, capture.seals, err)
				}
				if err := lane.Rollback(ctx); err != nil {
					t.Fatal(err)
				}
				var left int
				err = tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM cheesetrap.reports WHERE report_id=$1)+
(SELECT count(*) FROM cheesetrap.report_evidence WHERE report_id=$1)+
(SELECT count(*) FROM cheesetrap.report_proofs WHERE report_id=$1)+
(SELECT count(*) FROM cheesetrap.report_proof_parts WHERE report_id=$1)`, report).Scan(&left)
				if err != nil || left != 0 {
					t.Fatal("failed copy retained partial report state", left, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if capture.copies != 2 || capture.seals != 1 || capture.copiesAtSeal != 1024 {
				t.Fatalf("1024 streamed members used %d copy statements, %d seals and %d copies before seal; want 2,1,1024", capture.copies, capture.seals, capture.copiesAtSeal)
			}
			var actual []domain.ReportEvidence
			if err := snapshot.EachEvidence(func(row domain.ReportEvidence) error { actual = append(actual, row); return nil }); err != nil {
				t.Fatal(err)
			}
			want, _ := json.Marshal(expected.Evidence)
			got, _ := json.Marshal(actual)
			if !bytes.Equal(got, want) {
				t.Fatal("streamed evidence contents, identity or order changed")
			}
			var parts [][]byte
			for start := 0; start < len(expected.Evidence); start += 32 {
				part, err := json.Marshal(expected.Evidence[start:min(start+32, len(expected.Evidence))])
				if err != nil {
					t.Fatal(err)
				}
				parts = append(parts, part)
			}
			var wantedHash, actualHash string
			if err := lane.QueryRow(ctx, `SELECT encode(cheesetrap.report_part_hash(payload ORDER BY ordinal),'hex') FROM unnest($1::bytea[]) WITH ORDINALITY parts(payload,ordinal)`, parts).Scan(&wantedHash); err != nil {
				t.Fatal(err)
			}
			if err := lane.QueryRow(ctx, `SELECT evidence_hash FROM cheesetrap.report_proofs WHERE report_id=$1 AND sealed`, report).Scan(&actualHash); err != nil || actualHash != wantedHash {
				t.Fatal("signed evidence part bytes or boundaries changed", err)
			}
		})
	}
}
