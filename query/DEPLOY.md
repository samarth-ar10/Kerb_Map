# Deploying the Kerb Map app (`kerbmap-app`)

Pattern from the starter kit's `deployment/deploy-app-no-registry` skill: public image `python:3.12-slim`, code from a
ConfigMap, credentials from a Secret, Ingress on the team host at `/app` (prefix stripped). Users open
https://workshop.thecosmoslabs.com and click **App**.

These commands are for a person to run on the lab VM, from the repo root.
`kerbmap-app` is already on the cluster. A code change uses the update steps at the bottom.

## 0. Rehearse first (touches nothing shared)

```bash
bash query/rehearse_deploy.sh          # must end with "PASS: deploy rehearsal ..."
```
On the lab VM (no ensurepip) point it at a pip wheel and offline wheels if PyPI is not reachable:
`PIP_WHEEL=/tmp/wheels/pip-<ver>.whl PIP_FIND_LINKS=/tmp/wheels bash query/rehearse_deploy.sh`.

## 1. Settings (read from the team config, never printed)

```bash
CFG=$(find /config -maxdepth 1 -type f -name '*.config' | sort | head -1)
USERNAME=$(grep '^USERNAME=' "$CFG" | cut -d= -f2-)
NS="$USERNAME"                                   # team-5
export KUBECONFIG=/config/${NS}-k8s.yaml
APP_NAME=kerbmap-app
```

## 2. Build the flat code folder

```bash
python3 query/build_deploy.py                    # writes deploy/app/ and checks: flat, < 900 KB, relative URLs
```

## 3. ConfigMap (code)

```bash
kubectl -n "$NS" create configmap "${APP_NAME}-code" --from-file=deploy/app \
  --dry-run=client -o yaml | kubectl replace --force -f -
```
`kubectl apply` stores a second copy of the bundle in an annotation, and that copy exceeds the 256 KB annotation limit. `replace --force` writes the ConfigMap once.

## 4. Secret (VSS login, VastDB, optional GPU token for Cosmos Embed1 search)

```bash
kubectl -n "$NS" create secret generic "${APP_NAME}-vss-creds" \
  --from-literal=VSS_URL="$(grep '^INGRESS_URL=' "$CFG" | cut -d= -f2-)" \
  --from-literal=VSS_USERNAME="$USERNAME" \
  --from-literal=VSS_PASSWORD="$(grep '^PASSWORD=' "$CFG" | cut -d= -f2-)" \
  --from-literal=S3_ENDPOINT="$(grep '^S3_ENDPOINT=' "$CFG" | cut -d= -f2-)" \
  --from-literal=ACCESS_KEY="$(grep '^ACCESS_KEY=' "$CFG" | cut -d= -f2-)" \
  --from-literal=SECRET_KEY="$(grep '^SECRET_KEY=' "$CFG" | cut -d= -f2-)" \
  --from-literal=VASTDB_BUCKET="$(grep '^VASTDB_BUCKET=' "$CFG" | cut -d= -f2-)" \
  --from-literal=GPU_BEARER_TOKEN="$(grep '^GPU_BEARER_TOKEN=' "$CFG" | cut -d= -f2-)" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 5. Deployment + Service + Ingress

```bash
kubectl -n "$NS" apply -f deploy/k8s/kerbmap-app.yaml
```
The manifest sets `KERB_DB=vastdb` (tables in schema `kerbmap` of the team bucket; created on first start if missing)
and the Ingress host `video-lab-team-5.cosmos.vastdata.com`, path `/app(/|$)(.*)`, rewrite `/$2`.

## 6. Verify

```bash
kubectl -n "$NS" rollout status deploy/"$APP_NAME"
kubectl -n "$NS" get pods,svc,ingress -l app="$APP_NAME"
kubectl -n "$NS" logs -l app="$APP_NAME" --tail=100
curl -sS -o /dev/null -w "%{http_code}\n" "http://video-lab-team-5.cosmos.vastdata.com/app/"
curl -sS "http://video-lab-team-5.cosmos.vastdata.com/app/health"
```
Then open https://workshop.thecosmoslabs.com and click **App**.

## Update after a code change

```bash
python3 query/build_deploy.py
kubectl -n "$NS" create configmap "${APP_NAME}-code" --from-file=deploy/app --dry-run=client -o yaml | kubectl replace --force -f -
kubectl -n "$NS" rollout restart deploy/"$APP_NAME"
```

## Rollback (remove everything this added)

```bash
kubectl -n "$NS" delete ingress/"$APP_NAME" service/"$APP_NAME" deploy/"$APP_NAME" \
  configmap/"${APP_NAME}-code" secret/"${APP_NAME}-vss-creds"
```

## Notes

- One replica: labelling batch locks are held in the app's memory (labels themselves are rows in VastDB).
- Evidence: s3:// frames and clips are passed through the app from the VSS stream endpoint (server-side login), or from
  a presigned S3 GET as a fallback. No video decoding in the pod. Clips play in a `<video>` at `#t=start,end`.
- The pod has no `/config`; everything comes from the Secret above.
