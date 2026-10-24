"""One logical account per verified email; retain original sessions and job IDs."""
import hashlib
import re

def aliases(service):
    files=service._cookie_files()
    with service.connect() as db:
        details={r['account_key']:dict(r) for r in db.execute('select * from console_account_details')}
        profiles=dict(db.execute('select account_key,profile_name from console_account_profiles'))
    groups={}
    for key,path in files.items():
        row=details.get(key,{})
        if (row.get('email') and row.get('error')!='identity' and
                row.get('bundle_digest')==hashlib.sha256(path.read_bytes()).hexdigest()):
            groups.setdefault(row['email'].strip().casefold(),[]).append(key)
    result={}
    for email,keys in groups.items():
        def rank(key):
            name=profiles.get(key,'')
            valid=bool(re.fullmatch(r'flow_browser_profile_\d+',name) and
                (service.engine.root/'data'/name).is_dir() and not (service.engine.root/'data'/name).is_symlink())
            if valid:
                _, actual=service._profile_identity(name)
                valid=not actual or actual.strip().casefold()==email
            return (not valid, int(name.rsplit('_',1)[-1]) if valid else 10**20,key)
        canonical=min(keys,key=rank)
        result.update({key:canonical for key in keys})
    return result

def canonical(service,key):
    return aliases(service).get(key,key)
