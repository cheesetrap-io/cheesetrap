package reporting

import (
	"context"
	"encoding/json"
	"net/netip"
	"strconv"

	"cheesetrap/internal/domain"
	"cheesetrap/internal/store/db"
	"github.com/jackc/pgx/v5"
)

type storedContents struct {
	ctx                     context.Context
	q                       *db.Queries
	report, revision        string
	proof                   bool
	evidence, rules         int
	nullEvidence, nullRules bool
}

func (s storedContents) Counts() (int, int)       { return s.evidence, s.rules }
func (s storedContents) NullArrays() (bool, bool) { return s.nullEvidence, s.nullRules }
func (s storedContents) EachEvidence(f func(domain.ReportEvidence) error) error {
	return visitContents(s, "evidence", f)
}
func (s storedContents) EachRule(f func(domain.ReportRule) error) error {
	return visitContents(s, "rules", f)
}

func visitContents[T any](s storedContents, kind string, f func(T) error) error {
	if s.proof {
		var after int64 = -1
		seen := 0
		expected := s.evidence
		if kind == "rules" {
			expected = s.rules
		}
		for {
			type part struct {
				Ordinal int64
				Payload []byte
			}
			// Exploit explanations can contain every distinct retained URI and
			// span several MiB each. Bound bytes before returning payloads, not
			// only row count. A single oversized part is still read completely.
			// Carry payloads through the bounded candidate set, avoiding a second
			// table lookup that can repeatedly scan an entire proof prefix when
			// freshly written parts lack refreshed planner statistics.
			rows, err := s.q.Queryer().Query(s.ctx, `WITH candidates AS MATERIALIZED (
SELECT ordinal,payload,octet_length(payload) AS bytes FROM cheesetrap.report_proof_parts
WHERE report_id=$1 AND kind=$2 AND ordinal>$3 ORDER BY ordinal LIMIT 32
), budgeted AS (
SELECT ordinal,payload,sum(bytes) OVER (ORDER BY ordinal) AS total_bytes FROM candidates
)
SELECT ordinal,payload FROM budgeted
WHERE total_bytes<=1048576 OR ordinal=(SELECT min(ordinal) FROM candidates)
ORDER BY ordinal`, s.report, kind, after)
			if err != nil {
				return err
			}
			parts, err := pgx.CollectRows(rows, pgx.RowToStructByName[part])
			if err != nil {
				return err
			}
			if len(parts) == 0 {
				if seen != expected {
					return ErrRevisionFailed
				}
				return nil
			}
			for _, part := range parts {
				var page []T
				if err = json.Unmarshal(part.Payload, &page); err != nil {
					return err
				}
				if part.Ordinal != after+1 || len(page) == 0 || seen+len(page) > expected {
					return ErrRevisionFailed
				}
				seen += len(page)
				for _, value := range page {
					if err = f(value); err != nil {
						return err
					}
				}
				after = part.Ordinal
			}
		}
	}
	// Retained legacy arrays are paged in place. They are never reconstructed
	// from current events, which may already have been retired.
	return s.q.VisitLegacyReportContents(s.ctx, s.revision, kind, func(data []byte) error {
		var value T
		if err := json.Unmarshal(data, &value); err != nil {
			return err
		}
		return f(value)
	})
}

func readCurrentRevision(ctx context.Context, q *db.Queries, id string) (domain.ReportRevision, error) {
	// Large legacy arrays stay on the server; new revisions already contain
	// only a small manifest. NULL and empty arrays retain distinct v1 digests.
	rows, err := q.Queryer().Query(ctx, `SELECT v.revision_id,v.report_id,v.version,
(v.snapshot-'evidence'-'rules')||jsonb_build_object('_evidence_count',coalesce(jsonb_array_length(nullif(v.snapshot->'evidence','null')),0),'_rule_count',coalesce(jsonb_array_length(nullif(v.snapshot->'rules','null')),0),'_null_evidence',coalesce(v.snapshot->'evidence','null')='null'::jsonb,'_null_rules',coalesce(v.snapshot->'rules','null')='null'::jsonb) AS snapshot,
v.subject,v.body,v.recipient,v.reply_to,v.hold_reasons,v.state,v.auto_approval_allowed,v.body_edited,v.content_digest,v.created_at
FROM cheesetrap.report_revisions v JOIN cheesetrap.reports r ON r.current_revision_id=v.revision_id AND r.report_id=v.report_id WHERE r.report_id=$1`, id)
	if err != nil {
		return domain.ReportRevision{}, err
	}
	row, err := pgx.CollectExactlyOneRow(rows, pgx.RowToStructByName[db.CheesetrapReportRevision])
	if err != nil {
		return domain.ReportRevision{}, err
	}
	r, err := readRevision(row)
	if err != nil {
		return r, err
	}
	var meta struct {
		Evidence     int  `json:"_evidence_count"`
		Rules        int  `json:"_rule_count"`
		NullEvidence bool `json:"_null_evidence"`
		NullRules    bool `json:"_null_rules"`
	}
	if err = json.Unmarshal(row.Snapshot, &meta); err != nil {
		return r, err
	}
	source := storedContents{ctx: ctx, q: q, report: id, revision: r.RevisionID, evidence: meta.Evidence, rules: meta.Rules, nullEvidence: meta.NullEvidence, nullRules: meta.NullRules}
	if r.Snapshot.Proof != nil {
		source.proof = true
		source.evidence = r.Snapshot.Proof.EvidenceCount
		source.rules = r.Snapshot.Proof.RuleCount
	}
	r.Snapshot.Contents = source
	return r, nil
}

type proofWriter struct {
	ctx               context.Context
	q                 *db.Queries
	report            string
	offsets, ordinals map[string]int
	pending           []proofPart
	pendingBytes      int
	err               error
	sealed            bool
}

type proofPart struct {
	kind           string
	ordinal, first int64
	count          int32
	payload        []byte
}

func newProofWriter(ctx context.Context, q *db.Queries, report string) (*proofWriter, error) {
	_, err := q.Queryer().Exec(ctx, `INSERT INTO cheesetrap.report_proofs(report_id) VALUES($1)`, report)
	return &proofWriter{ctx: ctx, q: q, report: report, offsets: map[string]int{}, ordinals: map[string]int{}}, err
}
func (w *proofWriter) part(kind string, values any, count int) error {
	if w.err != nil {
		return w.err
	}
	if w.sealed || count < 0 || count > 32 {
		w.err = ErrRevisionFailed
		return w.err
	}
	if count == 0 {
		return nil
	}
	data, err := json.Marshal(values)
	if err != nil {
		w.err = err
		return w.err
	}
	// Keep every signed part's bytes and boundaries. Only the SQL request is
	// batched: at most 32 parts and 1 MiB, or one complete oversized part.
	if len(w.pending) > 0 && w.pendingBytes+len(data) > 1<<20 {
		if err := w.flush(); err != nil {
			return err
		}
	}
	w.pending = append(w.pending, proofPart{kind: kind, ordinal: int64(w.ordinals[kind]),
		first: int64(w.offsets[kind]), count: int32(count), payload: data})
	w.pendingBytes += len(data)
	w.ordinals[kind]++
	w.offsets[kind] += count
	if len(w.pending) == 32 || w.pendingBytes >= 1<<20 {
		return w.flush()
	}
	return nil
}

func (w *proofWriter) flush() error {
	if w.err != nil || len(w.pending) == 0 {
		return w.err
	}
	kinds := make([]string, len(w.pending))
	ordinals, firsts := make([]int64, len(w.pending)), make([]int64, len(w.pending))
	counts, payloads := make([]int32, len(w.pending)), make([][]byte, len(w.pending))
	for i, part := range w.pending {
		kinds[i], ordinals[i], firsts[i], counts[i], payloads[i] = part.kind, part.ordinal, part.first, part.count, part.payload
	}
	tag, err := w.q.Queryer().Exec(w.ctx, `INSERT INTO cheesetrap.report_proof_parts(report_id,kind,ordinal,first_item,item_count,payload)
SELECT $1::uuid,p.kind,p.ordinal,p.first_item,p.item_count,p.payload
FROM unnest($2::text[],$3::bigint[],$4::bigint[],$5::integer[],$6::bytea[])
WITH ORDINALITY AS p(kind,ordinal,first_item,item_count,payload,input_order)
ORDER BY p.input_order`, w.report, kinds, ordinals, firsts, counts, payloads)
	if err == nil && tag.RowsAffected() != int64(len(w.pending)) {
		err = ErrRevisionFailed
	}
	if err != nil {
		w.err = err
		return err
	}
	clear(w.pending)
	w.pending = w.pending[:0]
	w.pendingBytes = 0
	return nil
}
func (w *proofWriter) seal(snapshot *domain.ReportSnapshot) error {
	if err := w.flush(); err != nil {
		return err
	}
	var data []byte
	if err := w.q.Queryer().QueryRow(w.ctx, `SELECT cheesetrap.seal_report_proof($1)`, w.report).Scan(&data); err != nil {
		return err
	}
	var proof domain.ReportProof
	if err := json.Unmarshal(data, &proof); err != nil {
		return err
	}
	snapshot.Proof = &proof
	snapshot.Evidence = nil
	snapshot.Rules = nil
	snapshot.Contents = storedContents{ctx: w.ctx, q: w.q, report: w.report, proof: true, evidence: proof.EvidenceCount, rules: proof.RuleCount}
	w.sealed = true
	return nil
}

// Drafting traverses each SQL result once with a server cursor. Every copy,
// manifest and revision commits together under the existing ownership lock.
func collectProof(ctx context.Context, q *db.Queries, w db.CheesetrapReportWindow, source, report string) (domain.ReportSnapshot, error) {
	snapshot := domain.ReportSnapshot{SourceIP: source, WindowID: w.WindowID.String(), WindowStart: w.StartAt.Time.UTC(), WindowEnd: w.EndAt.Time.UTC()}
	writer, err := newProofWriter(ctx, q, report)
	if err != nil {
		return snapshot, err
	}
	err = q.VisitReportSourceDetections(ctx, db.ListReportSourceDetectionsParams{WindowID: w.WindowID, SourceIp: netip.MustParseAddr(source)}, func(page []db.ListReportSourceDetectionsRow) error {
		for _, d := range page {
			r := domain.ReportRule{EvidenceHashScheme: d.EvidenceHashScheme, SettingsVersion: strconv.FormatInt(d.SettingsVersion, 10), DetectionID: d.DetectionID.String(), RuleID: d.RuleID, RuleVersion: d.RuleVersion, SignatureID: d.SignatureID, SignatureVersion: d.SignatureVersion, ContextCount: int(d.EvidenceCount), AttachedCount: int(d.AttachedCount), EvidenceHash: d.EvidenceHash, EvidenceEligible: d.EvidenceEligible, AutoApprovalAllowed: d.AutoApprovalAllowed, Explanation: d.Explanation}
			if err := writer.part("rules", []domain.ReportRule{r}, 1); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return snapshot, err
	}
	gapForSensor := reportGapLookup(ctx, q, w)
	err = q.VisitReportCandidateEvidence(ctx, db.ListReportCandidateEvidenceParams{WindowID: w.WindowID, SourceIp: netip.MustParseAddr(source)}, func(page []db.CheesetrapEvent) error {
		copied := make([]domain.ReportEvidence, 0, len(page))
		for _, row := range page {
			e, err := copyEvidence(row)
			if err != nil {
				return err
			}
			e.CollectionGap, err = gapForSensor(row.SensorID)
			if err != nil {
				return err
			}
			copied = append(copied, e)
			if !e.ObservedAt.IsZero() {
				if snapshot.ObservedStart.IsZero() || e.ObservedAt.Before(snapshot.ObservedStart) {
					snapshot.ObservedStart = e.ObservedAt
				}
				if snapshot.ObservedEnd.IsZero() || e.ObservedAt.After(snapshot.ObservedEnd) {
					snapshot.ObservedEnd = e.ObservedAt
				}
			}
			if e.ObservedAt.Before(snapshot.WindowStart) {
				snapshot.DelayedCollection = true
			}
		}
		if err := copyReportEvidence(ctx, q, uuid(report), copied); err != nil {
			return err
		}
		return writer.part("evidence", copied, len(copied))
	})
	if err != nil {
		return snapshot, err
	}
	if writer.offsets["evidence"] == 0 {
		return snapshot, nil
	}
	err = writer.seal(&snapshot)
	return snapshot, err
}

// An edit of an old revision creates new parts. It never rewrites historical
// JSON or its signed digest, and the newly signed manifest uses complete copies.
func normalizeLegacyProof(ctx context.Context, q *db.Queries, r *domain.ReportRevision) error {
	if r.Snapshot.Proof != nil || r.Snapshot.Contents == nil {
		return nil
	}
	w, err := newProofWriter(ctx, q, r.ReportID)
	if err != nil {
		return err
	}
	if err = r.Snapshot.EachRule(func(v domain.ReportRule) error { return w.part("rules", []domain.ReportRule{v}, 1) }); err != nil {
		return err
	}
	if err = r.Snapshot.EachEvidence(func(v domain.ReportEvidence) error { return w.part("evidence", []domain.ReportEvidence{v}, 1) }); err != nil {
		return err
	}
	return w.seal(&r.Snapshot)
}
