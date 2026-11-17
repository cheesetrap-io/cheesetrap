//go:build integration

package reporting

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"cheesetrap/internal/domain"
	"cheesetrap/internal/store"
	"cheesetrap/internal/store/db"
	"github.com/jackc/pgx/v5"
)

func TestProofWriteBatchesPreserveSignedBytesAndRollback(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := store.NewPool(ctx, os.Getenv("CHEESETRAP_TEST_PROOF_MIGRATION_URL"))
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	windowNumber := 0
	newReport := func(tx pgx.Tx) string {
		t.Helper()
		at := time.Date(2003, 1, 1, 0, 0, 0, 0, time.UTC).Add(time.Duration(windowNumber) * time.Hour)
		windowNumber++
		var id string
		err := tx.QueryRow(ctx, `WITH w AS (
INSERT INTO cheesetrap.report_windows(start_at,end_at,settings_version,state,sealed_seq,sealed_at,ready_at)
VALUES($1,$2,1,'ready',0,$2,$2) RETURNING window_id)
INSERT INTO cheesetrap.reports(window_id,source_ip) SELECT window_id,'1.1.1.1' FROM w RETURNING report_id::text`, at, at.Add(time.Hour)).Scan(&id)
		if err != nil {
			t.Fatal(err)
		}
		return id
	}
	id := newReport(tx)
	w, err := newProofWriter(ctx, db.New(tx), id)
	if err != nil {
		t.Fatal(err)
	}
	var expected [][]byte
	for i := 0; i < 66; i++ {
		rule := domain.ReportRule{RuleID: fmt.Sprintf("fixture-%02d", i), Explanation: json.RawMessage(`{"path":"<retained>"}`)}
		if i == 32 {
			rule.Explanation, _ = json.Marshal(map[string]string{"wide": strings.Repeat("x", (1<<20)+1)})
		}
		payload, err := json.Marshal([]domain.ReportRule{rule})
		if err != nil {
			t.Fatal(err)
		}
		expected = append(expected, payload)
		if err := w.part("rules", []domain.ReportRule{rule}, 1); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.part("evidence", []domain.ReportEvidence{{EventID: "10000000-0000-4000-8000-000000000001"}}, 1); err != nil {
		t.Fatal(err)
	}
	var snapshot domain.ReportSnapshot
	if err := w.seal(&snapshot); err != nil {
		t.Fatal(err)
	}
	var expectedHash, actualHash string
	if err := tx.QueryRow(ctx, `SELECT encode(cheesetrap.report_part_hash(payload ORDER BY ordinal),'hex')
FROM unnest($1::bytea[]) WITH ORDINALITY AS parts(payload,ordinal)`, expected).Scan(&expectedHash); err != nil {
		t.Fatal(err)
	}
	if err := tx.QueryRow(ctx, `SELECT rules_hash FROM cheesetrap.report_proofs WHERE report_id=$1 AND sealed`, id).Scan(&actualHash); err != nil || actualHash != expectedHash {
		t.Fatal("signed part hash changed", err)
	}
	rows, err := tx.Query(ctx, `SELECT ordinal,first_item,item_count,payload FROM cheesetrap.report_proof_parts WHERE report_id=$1 AND kind='rules' ORDER BY ordinal`, id)
	if err != nil {
		t.Fatal(err)
	}
	seen := 0
	for rows.Next() {
		var ordinal, first int64
		var count int
		var payload []byte
		if err := rows.Scan(&ordinal, &first, &count, &payload); err != nil {
			rows.Close()
			t.Fatal(err)
		}
		if seen >= len(expected) || ordinal != int64(seen) || first != int64(seen) || count != 1 || !bytes.Equal(payload, expected[seen]) {
			rows.Close()
			t.Fatal("stored proof bytes/order changed", seen)
		}
		seen++
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	rows.Close()
	if seen != 66 || snapshot.Proof.RuleCount != 66 || snapshot.Proof.EvidenceCount != 1 {
		t.Fatal("proof tail or counts lost", seen)
	}

	// Fail a later statement after the first 32 parts reached PostgreSQL. The
	// caller's rollback must remove the report, manifest and earlier parts.
	failed, err := tx.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	failedID := newReport(failed)
	failing, err := newProofWriter(ctx, db.New(failed), failedID)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 32; i++ {
		if err := failing.part("rules", []map[string]int{{"id": i}}, 1); err != nil {
			t.Fatal(err)
		}
	}
	var written int
	if err := failed.QueryRow(ctx, `SELECT count(*) FROM cheesetrap.report_proof_parts WHERE report_id=$1`, failedID).Scan(&written); err != nil || written != 32 {
		t.Fatal("rollback probe needs an earlier successful statement", written, err)
	}
	if err := failing.part("invalid-kind", []map[string]int{{"id": 99}}, 1); err != nil {
		t.Fatal(err)
	}
	if err := failing.seal(&domain.ReportSnapshot{}); err == nil {
		t.Fatal("invalid later statement accepted")
	}
	if err := failed.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	var reports, proofs, parts int
	if err := tx.QueryRow(ctx, `SELECT (SELECT count(*) FROM cheesetrap.reports WHERE report_id=$1),
(SELECT count(*) FROM cheesetrap.report_proofs WHERE report_id=$1),
(SELECT count(*) FROM cheesetrap.report_proof_parts WHERE report_id=$1)`, failedID).Scan(&reports, &proofs, &parts); err != nil || reports != 0 || proofs != 0 || parts != 0 {
		t.Fatal("later failure retained partial proof", reports, proofs, parts, err)
	}
	if err := tx.QueryRow(ctx, `SELECT rules_hash FROM cheesetrap.report_proofs WHERE report_id=$1 AND sealed`, id).Scan(&actualHash); err != nil || actualHash != expectedHash {
		t.Fatal("rollback affected a separate complete proof", err)
	}
}
