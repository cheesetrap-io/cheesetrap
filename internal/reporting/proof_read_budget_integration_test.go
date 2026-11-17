//go:build integration

package reporting

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"cheesetrap/internal/domain"
	"cheesetrap/internal/store"
	"cheesetrap/internal/store/db"
	"github.com/jackc/pgx/v5"
)

func TestProofReadsBoundPhysicalWorkBeforeStatisticsRefresh(t *testing.T) {
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
	var id string
	err = tx.QueryRow(ctx, `WITH w AS (
INSERT INTO cheesetrap.report_windows(start_at,end_at,settings_version,state,sealed_seq,sealed_at,ready_at)
VALUES('2005-01-01','2005-01-02',1,'ready',0,'2005-01-02','2005-01-02') RETURNING window_id)
INSERT INTO cheesetrap.reports(window_id,source_ip) SELECT window_id,'1.1.1.1' FROM w RETURNING report_id::text`).Scan(&id)
	if err != nil {
		t.Fatal(err)
	}
	capture := &proofReadCapture{Tx: tx}
	writer, err := newProofWriter(ctx, db.New(capture), id)
	if err != nil {
		t.Fatal(err)
	}
	const count = 12000
	for i := 0; i < count; i++ {
		rule := domain.ReportRule{RuleID: fmt.Sprintf("rule-%05d", i), SettingsVersion: "1", Explanation: json.RawMessage(`{"count":1}`)}
		if err := writer.part("rules", []domain.ReportRule{rule}, 1); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.part("evidence", []domain.ReportEvidence{{EventID: "10000000-0000-4000-8000-000000000001"}}, 1); err != nil {
		t.Fatal(err)
	}
	var snapshot domain.ReportSnapshot
	if err := writer.seal(&snapshot); err != nil {
		t.Fatal(err)
	}
	// Freshly drafted parts are still uncommitted, so background ANALYZE cannot
	// refresh their statistics. Exercise the exact reader and capture its SQL.
	stop := errors.New("first page inspected")
	seen := 0
	err = snapshot.EachRule(func(rule domain.ReportRule) error {
		if rule.RuleID != fmt.Sprintf("rule-%05d", seen) {
			t.Fatal("proof order changed", seen)
		}
		seen++
		if seen == 32 {
			return stop
		}
		return nil
	})
	if !errors.Is(err, stop) || seen != 32 || capture.sql == "" {
		t.Fatal("first proof page or callback failure lost", seen, err)
	}
	for _, after := range []int64{-1, count - 33} {
		args := append([]any(nil), capture.args...)
		args[2] = after
		var raw []byte
		if err := tx.QueryRow(ctx, "EXPLAIN (ANALYZE, FORMAT JSON) "+capture.sql, args...).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		var plan []struct{ Plan proofReadPlan }
		if err := json.Unmarshal(raw, &plan); err != nil || len(plan) != 1 {
			t.Fatal("missing actual reader plan", err)
		}
		visited := plan[0].Plan.proofRowsVisited()
		t.Logf("proof parts=%d after=%d selected page=%d relation rows*loops=%.0f", count, after, seen, visited)
		if visited > 1024 {
			t.Fatalf("bounded proof page visited %.0f physical rows; want <=1024 independent of total %d", visited, count)
		}
	}
	seen = 0
	err = snapshot.EachRule(func(rule domain.ReportRule) error {
		if rule.RuleID != fmt.Sprintf("rule-%05d", seen) || string(rule.Explanation) != `{"count":1}` {
			t.Fatal("complete proof content or order changed", seen)
		}
		seen++
		return nil
	})
	if err != nil || seen != count {
		t.Fatal("proof tail was lost", seen, err)
	}
}

type proofReadCapture struct {
	pgx.Tx
	sql  string
	args []any
}

func (c *proofReadCapture) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	c.sql, c.args = sql, append([]any(nil), args...)
	return c.Tx.Query(ctx, sql, args...)
}

type proofReadPlan struct {
	Relation string          `json:"Relation Name"`
	Rows     float64         `json:"Actual Rows"`
	Loops    float64         `json:"Actual Loops"`
	Filtered float64         `json:"Rows Removed by Filter"`
	Plans    []proofReadPlan `json:"Plans"`
}

func (n proofReadPlan) proofRowsVisited() float64 {
	var visited float64
	if n.Relation == "report_proof_parts" {
		visited = (n.Rows + n.Filtered) * n.Loops
	}
	for _, child := range n.Plans {
		visited += child.proofRowsVisited()
	}
	return visited
}
