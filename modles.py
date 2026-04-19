import google.generativeai as genai
genai.configure(api_key="AIzaSyCG3ZUhb7J1VdzKL6TSwPtJ-pTBqaGtCnU")
for m in genai.list_models():
    print(m.name)
