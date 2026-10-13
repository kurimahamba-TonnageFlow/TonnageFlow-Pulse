BEGIN;
CREATE TABLE public.shift_report_snapshots (
 id uuid PRIMARY KEY,
 report_type text NOT NULL CHECK (report_type IN ('linetech','engineering','management')),
 production_line text NOT NULL,
 period_start timestamptz NOT NULL,
 period_end timestamptz NOT NULL CHECK (period_end>period_start),
 generated_at timestamptz NOT NULL,
 actor_role text NOT NULL CHECK (actor_role IN ('linetech','engineering','management')),
 actor_name text NOT NULL,
 revision_of uuid REFERENCES public.shift_report_snapshots(id),
 root_id uuid NOT NULL,
 version integer NOT NULL CHECK (version>0),
 request_key uuid NOT NULL,
 fingerprint text NOT NULL,
 snapshot jsonb NOT NULL,
 UNIQUE(actor_role,actor_name,request_key),
 UNIQUE(root_id,version)
);
CREATE INDEX shift_report_history ON public.shift_report_snapshots(report_type, generated_at DESC);
ALTER TABLE public.shift_report_snapshots ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.shift_report_snapshots FROM PUBLIC, anon, authenticated;
CREATE FUNCTION public.immutable_shift_report() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Report snapshots are immutable; generate a revision.'; END $$;
CREATE TRIGGER immutable_shift_report BEFORE UPDATE OR DELETE ON public.shift_report_snapshots
FOR EACH ROW EXECUTE FUNCTION public.immutable_shift_report();
REVOKE ALL ON FUNCTION public.immutable_shift_report() FROM PUBLIC, anon, authenticated;
COMMIT;
