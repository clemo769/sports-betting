from src.linking.match_linker import link_matches
import pandas as pd
rows=[]
rows.append({'source':'fdco','source_id':'r7','league':'championship','season':'23/24','home_team':'Hull','away_team':'Swansea','match_date_utc':'2024-05-05T14:00:00Z'})
rows.append({'source':'other','source_id':'o7','league':'premier-league','season':'23/24','home_team':'Hull City','away_team':'Swansea City','match_date_utc':'2024-05-05T14:00:00Z'})
df=pd.DataFrame(rows)
linked=link_matches(df)
print(linked.to_string(index=False))
